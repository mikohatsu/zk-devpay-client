# app_ui.py — zk-DevPay local metering bridge (Module 1)
#
# Start: silently route local OpenAI-compatible traffic through a loopback
# proxy, encrypt prompts for the core session, heartbeat token totals to BFF.
# Stop: restore the developer's previous API-base settings.
#
# API keys are never read from disk, never stored, and never logged — they only
# transit in-memory from the IDE/tool request to the upstream provider.
from __future__ import annotations

import sys
import os
import json
import threading
import time
import base64
import secrets
import asyncio
import re
import requests
from pathlib import Path
from typing import Optional, Any
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QFrame,
    QHBoxLayout,
    QComboBox,
    QCheckBox,
)
from PyQt6.QtCore import pyqtSignal, QObject, QTimer, Qt
from PyQt6.QtGui import QFont
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import serialization, hashes
import litellm
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from uvicorn import Config, Server

# ---------------------------------------------------------------------------
# Config (service URLs only — never load provider API keys)
# ---------------------------------------------------------------------------

_PROVIDER_KEY_RE = re.compile(r"(API_KEY|ACCESS_TOKEN|SECRET|PASSWORD)$", re.I)

if os.path.exists(".env"):
    with open(".env", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                if _PROVIDER_KEY_RE.search(k):
                    continue  # never ingest secrets into this process
                os.environ.setdefault(k, v)

# Scrub any provider secrets that leaked in from the parent shell
for _k in list(os.environ):
    if _PROVIDER_KEY_RE.search(_k):
        os.environ.pop(_k, None)

WEB_BFF_URL = os.getenv("WEB_BFF_URL", "http://localhost:8081")
CORE_API_URL = os.getenv("CORE_API_URL", "http://localhost:8080")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "4000"))
STATE_PATH = Path(os.getenv("ZK_DEVPAY_STATE", ".zk-devpay-device.json"))
ROUTE_BACKUP_PATH = Path(os.getenv("ZK_DEVPAY_ROUTE_BACKUP", ".zk-devpay-route-backup.json"))
METER_HOOK_URL = f"http://127.0.0.1:{GATEWAY_PORT}/internal/meter"

# Env vars IDEs/CLIs commonly use for OpenAI-compatible base URLs
_ROUTE_ENV_KEYS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_API_BASE_URL",
    "GEMINI_API_BASE",
    "GOOGLE_GEMINI_BASE_URL",
    "GOOGLE_API_BASE",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_BASE",
    "CLAUDE_API_BASE",
    "LITELLM_API_BASE",
    "AZURE_API_BASE",
)

# Cursor / VS Code settings keys that may hold a custom API base
_EDITOR_BASE_KEYS = (
    "openai.baseUrl",
    "openai.apiBase",
    "cursor.openai.baseUrl",
    "cursor.general.openaiApiBase",
    "continue.openaiApiBase",
)

litellm.suppress_debug_info = True
litellm.telemetry = False

# ---------------------------------------------------------------------------
# Persistent device token
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def clear_device_token() -> None:
    state = load_state()
    state.pop("deviceToken", None)
    save_state(state)


# ---------------------------------------------------------------------------
# Silent local routing (apply on Start, restore on Stop)
# ---------------------------------------------------------------------------

def _gateway_base() -> str:
    return f"http://127.0.0.1:{GATEWAY_PORT}/v1"


def _win_user_env_get(name: str) -> Optional[str]:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            try:
                val, _ = winreg.QueryValueEx(key, name)
                return str(val)
            except FileNotFoundError:
                return None
    except Exception:
        return None


def _win_user_env_set(name: str, value: Optional[str]) -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg
        import ctypes

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ, value)
        # Notify other processes (best-effort; already-running apps may need restart)
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None
        )
    except Exception:
        pass


def _editor_settings_paths() -> list:
    paths = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            paths.append(Path(appdata) / "Cursor" / "User" / "settings.json")
            paths.append(Path(appdata) / "Code" / "User" / "settings.json")
    else:
        home = Path.home()
        paths.append(home / ".config" / "Cursor" / "User" / "settings.json")
        paths.append(home / "Library" / "Application Support" / "Cursor" / "User" / "settings.json")
        paths.append(home / ".config" / "Code" / "User" / "settings.json")
    return paths


def _patch_editor_settings(target_base: str) -> list:
    """Point known editor API-base keys at the local gateway. Returns backup entries."""
    backups = []
    for path in _editor_settings_paths():
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            # Allow JSONC-ish trailing comments by stripping // lines lightly
            data = json.loads(raw)
        except Exception:
            continue
        changed = False
        entry = {"path": str(path), "keys": {}}
        for key in _EDITOR_BASE_KEYS:
            if key in data:
                entry["keys"][key] = data.get(key)
                data[key] = target_base
                changed = True
        # If none of the keys existed, set the most common one so OpenAI-compatible
        # clients that read it will pick up the bridge without asking the user.
        if not entry["keys"]:
            entry["keys"]["openai.baseUrl"] = data.get("openai.baseUrl")  # may be None
            data["openai.baseUrl"] = target_base
            changed = True
        if changed:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            backups.append(entry)
    return backups


def _restore_editor_settings(backups: list) -> None:
    for entry in backups or []:
        path = Path(entry.get("path", ""))
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key, old in (entry.get("keys") or {}).items():
            if old is None:
                data.pop(key, None)
            else:
                data[key] = old
        try:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass


def apply_local_routing() -> None:
    """Redirect other apps' API base URLs to the loopback gateway.

    Important: do NOT set these on *this* process — LiteLLM would recurse into
    our own gateway. Only User-level env (Windows) + editor settings.json.
    """
    if ROUTE_BACKUP_PATH.exists():
        # Unclean previous stop — put the machine back first
        restore_local_routing()

    target = _gateway_base()
    backup: dict[str, Any] = {"env": {}, "editor": [], "target": target}

    for key in _ROUTE_ENV_KEYS:
        user_val = _win_user_env_get(key) if sys.platform == "win32" else None
        backup["env"][key] = {"user": user_val}
        if sys.platform == "win32":
            _win_user_env_set(key, target)

    backup["editor"] = _patch_editor_settings(target)
    ROUTE_BACKUP_PATH.write_text(json.dumps(backup, indent=2), encoding="utf-8")


def restore_local_routing() -> None:
    """Undo apply_local_routing(). Safe to call when no backup exists."""
    if not ROUTE_BACKUP_PATH.exists():
        return
    try:
        backup = json.loads(ROUTE_BACKUP_PATH.read_text(encoding="utf-8"))
    except Exception:
        ROUTE_BACKUP_PATH.unlink(missing_ok=True)
        return

    for key, slot in (backup.get("env") or {}).items():
        user_val = (slot or {}).get("user")
        if sys.platform == "win32":
            _win_user_env_set(key, user_val)

    _restore_editor_settings(backup.get("editor") or [])
    try:
        ROUTE_BACKUP_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Hybrid encryption (matches zk-devpay-server crypto.service.ts)
# ---------------------------------------------------------------------------

def encrypt_payload(public_key_pem: str, plaintext: str) -> str:
    public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    aes_key = secrets.token_bytes(32)
    iv = secrets.token_bytes(12)
    aesgcm = AESGCM(aes_key)
    ciphertext = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    auth_tag = ciphertext[-16:]
    body = ciphertext[:-16]
    encrypted_aes_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(encrypted_aes_key + iv + auth_tag + body).decode("ascii")


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

class BridgeSignals(QObject):
    token_updated = pyqtSignal(int)
    status_updated = pyqtSignal(str)
    assignment_updated = pyqtSignal(object)


signals = BridgeSignals()
current_tokens = 0
assignment = None  # type: Optional[dict]
_stop_heartbeat = threading.Event()
_uvicorn_server = None  # type: Optional[Server]
_bridge_running = False
_deep_session = None  # type: Optional[Any]
_metering_mode = "off"  # off | basic | deep


def get_metering_mode() -> str:
    state = load_state()
    return state.get("meteringMode") or _metering_mode or "off"


def set_metering_mode_local(mode: str, consent: bool = False) -> None:
    global _metering_mode
    _metering_mode = mode
    state = load_state()
    state["meteringMode"] = mode
    if mode == "deep" and consent:
        state["deepMeteringConsent"] = True
    if mode != "deep":
        state.pop("deepMeteringConsent", None)
    save_state(state)


def bff_headers(device_token: str) -> dict:
    return {"x-device-token": device_token, "Content-Type": "application/json"}


def fetch_assignment(device_token: str) -> Optional[dict]:
    res = requests.get(
        f"{WEB_BFF_URL}/api/client/assignment",
        headers=bff_headers(device_token),
        timeout=10,
    )
    res.raise_for_status()
    return res.json().get("assignment")


def post_usage(device_token: str, job_id: str, total_tokens: int) -> None:
    requests.post(
        f"{WEB_BFF_URL}/api/client/usage",
        headers=bff_headers(device_token),
        json={"jobId": job_id, "totalTokens": total_tokens},
        timeout=10,
    ).raise_for_status()


def post_encrypted_log(core_api_url: str, session_id: str, ciphertext: str, total_tokens: int) -> None:
    payload = {
        "sessionId": session_id,
        "encryptedPrompt": ciphertext,
        "tokenCount": {
            "promptTokens": total_tokens,
            "completionTokens": 0,
            "totalTokens": total_tokens,
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    res = requests.post(f"{core_api_url}/api/session/log", json=payload, timeout=15)
    res.raise_for_status()


def record_prompt_usage(raw_prompt: str, total_tokens: int) -> None:
    """Encrypt + upload one turn; bump live token counter. Never logs secrets."""
    global current_tokens, assignment
    try:
        if not assignment or not assignment.get("coreSessionId") or not assignment.get("rsaPublicKeyPem"):
            signals.status_updated.emit("Bridge ON · waiting for active job…")
            return
        if total_tokens <= 0:
            total_tokens = max(1, len(raw_prompt) // 4)

        current_tokens += total_tokens
        signals.token_updated.emit(current_tokens)

        ciphertext = encrypt_payload(assignment["rsaPublicKeyPem"], raw_prompt)
        core_url = assignment.get("coreApiUrl") or CORE_API_URL
        post_encrypted_log(core_url, assignment["coreSessionId"], ciphertext, total_tokens)
        signals.status_updated.emit(f"Metering · tokens {current_tokens:,}")
    except Exception as e:
        # Never include request headers / keys in status
        signals.status_updated.emit(f"Meter error: {type(e).__name__}")


def normalize_model(model: Optional[str]) -> str:
    if not model:
        return "openai/gpt-4o-mini"
    if "/" in model:
        return model
    m = model.lower()
    if m.startswith(("gpt-", "o1", "o3", "o4", "text-", "chatgpt")):
        return f"openai/{model}"
    if m.startswith("gemini") or m.startswith("models/gemini"):
        return f"gemini/{model.replace('models/', '')}"
    if m.startswith("claude"):
        return f"anthropic/{model}"
    if m.startswith(("command", "embed-")):
        return f"cohere/{model}"
    if m.startswith("mistral") or m.startswith("mixtral") or m.startswith("codestral"):
        return f"mistral/{model}"
    return f"openai/{model}"


def extract_request_api_key(request: Request) -> Optional[str]:
    """Ephemeral only — never written to disk or status text."""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    for header in ("x-api-key", "x-goog-api-key", "api-key"):
        val = (request.headers.get(header) or "").strip()
        if val:
            return val
    return None


def _response_to_dict(response: Any) -> dict:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if hasattr(response, "json"):
        raw = response.json()
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    if isinstance(response, dict):
        return response
    return dict(response)


def _usage_tokens(data: dict, fallback_text: str) -> int:
    usage = data.get("usage") or {}
    total = int(usage.get("total_tokens") or 0)
    if total > 0:
        return total
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if prompt or completion:
        return prompt + completion
    return max(1, len(fallback_text) // 4)


def _messages_prompt_text(messages: list) -> str:
    parts = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            # multimodal — only keep text parts for metering ciphertext
            content = " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict)
            )
        parts.append(f"{m.get('role', 'user')}: {content}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Loopback gateway — passthrough only (no local provider keys)
# ---------------------------------------------------------------------------

gateway_app = FastAPI(title="zk-DevPay metering bridge")


@gateway_app.get("/health")
def gateway_health():
    return {
        "status": "ok",
        "service": "zk-devpay-client-gateway",
        "metering": _bridge_running,
        "mode": get_metering_mode(),
    }


@gateway_app.post("/internal/meter")
async def internal_meter(request: Request):
    """Deep MITM addon callback — loopback only, never accepts remote."""
    if request.client and request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse(status_code=403, content={"error": "loopback_only"})
    body = await request.json()
    prompt = str(body.get("prompt") or "")
    tokens = int(body.get("totalTokens") or 0)
    # Never accept or store API keys from this hook.
    body.pop("apiKey", None)
    body.pop("authorization", None)
    record_prompt_usage(prompt, tokens)
    return {"status": "metered"}


async def _chat_completions_impl(request: Request):
    body = await request.json()
    model = normalize_model(body.get("model"))
    messages = body.get("messages") or []
    prompt_text = _messages_prompt_text(messages)
    stream = bool(body.get("stream"))
    api_key = extract_request_api_key(request)

    # Forward only the caller's credentials — never fall back to process env keys
    call_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    # Pass through common sampling fields if present
    for field in ("temperature", "top_p", "max_tokens", "n", "stop", "tools", "tool_choice"):
        if field in body:
            call_kwargs[field] = body[field]
    if api_key:
        call_kwargs["api_key"] = api_key

    try:
        if stream:
            return await _stream_completion(call_kwargs, prompt_text)
        response = await asyncio.to_thread(litellm.completion, **call_kwargs)
        data = _response_to_dict(response)
        record_prompt_usage(prompt_text, _usage_tokens(data, prompt_text))
        return JSONResponse(content=data)
    except Exception as e:
        signals.status_updated.emit(f"Upstream error: {type(e).__name__}")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "upstream_failed", "type": type(e).__name__}},
        )


async def _stream_completion(call_kwargs: dict, prompt_text: str):
    """Proxy SSE chunks; meter using final usage chunk or a text estimate."""

    def generate():
        collected = []
        usage_total = 0
        try:
            stream = litellm.completion(**call_kwargs)
            for chunk in stream:
                data = _response_to_dict(chunk) if not isinstance(chunk, dict) else chunk
                usage = data.get("usage") or {}
                if usage.get("total_tokens"):
                    usage_total = int(usage["total_tokens"])
                choices = data.get("choices") or []
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if delta:
                        collected.append(delta)
                yield f"data: {json.dumps(data)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            err = {"error": {"message": "upstream_failed", "type": type(e).__name__}}
            yield f"data: {json.dumps(err)}\n\n"
            yield "data: [DONE]\n\n"
            signals.status_updated.emit(f"Upstream error: {type(e).__name__}")
            return
        fallback = prompt_text + "".join(collected)
        record_prompt_usage(prompt_text, usage_total or max(1, len(fallback) // 4))

    return StreamingResponse(generate(), media_type="text/event-stream")


@gateway_app.post("/v1/chat/completions")
@gateway_app.post("/chat/completions")
async def chat_completions(request: Request):
    return await _chat_completions_impl(request)


def run_uvicorn():
    global _uvicorn_server
    config = Config(gateway_app, host="127.0.0.1", port=GATEWAY_PORT, log_level="warning")
    _uvicorn_server = Server(config)
    asyncio.run(_uvicorn_server.serve())


def heartbeat_loop(device_token: str):
    global assignment, current_tokens
    while not _stop_heartbeat.is_set():
        try:
            assignment = fetch_assignment(device_token)
            signals.assignment_updated.emit(assignment)
            mode = get_metering_mode()
            if assignment:
                remote = int(assignment.get("totalTokens") or 0)
                if remote > current_tokens:
                    current_tokens = remote
                    signals.token_updated.emit(current_tokens)
                remote_mode = assignment.get("meteringMode")
                if remote_mode in ("off", "basic", "deep"):
                    set_metering_mode_local(
                        remote_mode,
                        consent=bool(load_state().get("deepMeteringConsent")),
                    )
                if (
                    mode != "off"
                    and assignment.get("status") in ("assigned", "working")
                    and assignment.get("jobId")
                ):
                    post_usage(device_token, assignment["jobId"], current_tokens)
                signals.status_updated.emit(
                    f"Synced · mode={mode} · job {assignment.get('status')} · tokens {current_tokens:,}"
                )
            else:
                signals.status_updated.emit(f"Bridge ON ({mode}) · no active job")
        except Exception as e:
            signals.status_updated.emit(f"Heartbeat error: {type(e).__name__}")
        _stop_heartbeat.wait(30)


def link_device(device_code: str) -> str:
    res = requests.post(
        f"{WEB_BFF_URL}/api/client/link",
        json={"deviceCode": device_code},
        timeout=10,
    )
    res.raise_for_status()
    token = res.json()["deviceToken"]
    save_state({"deviceToken": token, "webBffUrl": WEB_BFF_URL})
    return token


def post_metering_mode(device_token: str, mode: str, deep_consent: bool = False) -> dict:
    payload = {"meteringMode": mode}
    if mode == "deep":
        payload["deepMeteringConsent"] = deep_consent
    res = requests.patch(
        f"{WEB_BFF_URL}/api/client/metering",
        headers=bff_headers(device_token),
        json=payload,
        timeout=10,
    )
    res.raise_for_status()
    return res.json()


def fetch_profile(device_token: str) -> Optional[dict]:
    res = requests.get(
        f"{WEB_BFF_URL}/api/client/me",
        headers=bff_headers(device_token),
        timeout=10,
    )
    res.raise_for_status()
    return res.json()


def unlink_device(device_token: str) -> None:
    try:
        requests.post(
            f"{WEB_BFF_URL}/api/client/unlink",
            headers=bff_headers(device_token),
            timeout=10,
        ).raise_for_status()
    finally:
        clear_device_token()


def format_elapsed(started_iso: Optional[str]) -> str:
    if not started_iso:
        return "00:00:00"
    try:
        text = started_iso.replace("Z", "+00:00")
        from datetime import datetime, timezone

        started = datetime.fromisoformat(text)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    except Exception:
        return "00:00:00"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class ZkDevPayApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("zk-DevPay Local Dashboard")
        self.setFixedSize(480, 600)
        self.setStyleSheet("background-color: #1e1e2e; color: #cdd6f4;")
        self._work_started_at = None
        self._profile = None

        signals.token_updated.connect(self.update_token_display)
        signals.status_updated.connect(self.update_status_bar)
        signals.assignment_updated.connect(self.on_assignment)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(25, 20, 25, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("zk-DevPay")
        title.setFont(QFont("Arial", 22, QFont.Weight.Bold))
        title.setStyleSheet("color: #ca9ee6;")
        header.addWidget(title)
        header.addStretch()
        self.btn_unlink = QPushButton("Unlink")
        self.btn_unlink.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_unlink.setFlat(True)
        self.btn_unlink.setStyleSheet(
            "color: #7f849c; font-size: 11px; padding: 2px 6px; border: none; background: transparent;"
        )
        self.btn_unlink.clicked.connect(self.on_unlink)
        header.addWidget(self.btn_unlink, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        self.account_card = QFrame()
        self.account_card.setStyleSheet("background-color: #252434; border-radius: 12px;")
        account_layout = QVBoxLayout(self.account_card)
        account_layout.setContentsMargins(14, 12, 14, 12)
        self.lbl_account = QLabel()
        self.lbl_account.setWordWrap(True)
        self.lbl_account.setStyleSheet("color: #cdd6f4; font-size: 13px;")
        account_layout.addWidget(self.lbl_account)
        layout.addWidget(self.account_card)

        card = QFrame()
        card.setStyleSheet("background-color: #252434; border-radius: 12px;")
        card_layout = QVBoxLayout(card)
        self.lbl_tokens = QLabel("Accumulated Tokens: 0")
        self.lbl_tokens.setFont(QFont("Arial", 14))
        self.lbl_usdc = QLabel("Pending Earnings: 0.0000 USDC")
        self.lbl_usdc.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        self.lbl_usdc.setStyleSheet("color: #a6e3a1;")
        self.lbl_job = QLabel("Assignment: —")
        self.lbl_job.setStyleSheet("color: #a6adc8;")
        self.lbl_job.setWordWrap(True)
        self.lbl_timer = QLabel("Work time: —")
        self.lbl_timer.setFont(QFont("Arial", 18, QFont.Weight.Bold))
        self.lbl_timer.setStyleSheet("color: #89b4fa;")
        card_layout.addWidget(self.lbl_tokens)
        card_layout.addWidget(self.lbl_usdc)
        card_layout.addWidget(self.lbl_job)
        card_layout.addWidget(self.lbl_timer)
        layout.addWidget(card)

        self.link_label = QLabel("Device code (from web My Page):")
        layout.addWidget(self.link_label)
        self.link_row = QWidget()
        row = QHBoxLayout(self.link_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.code_input = QLineEdit()
        self.code_input.setMaxLength(6)
        self.code_input.setPlaceholderText("6-digit code")
        self.code_input.setStyleSheet(
            "background-color: #2f2e41; padding: 8px; border-radius: 6px; color: #a6adc8;"
        )
        self.btn_link = QPushButton("Link")
        self.btn_link.setStyleSheet(
            "background-color: #89b4fa; color: #11111b; padding: 8px 14px; border-radius: 8px; font-weight: bold;"
        )
        self.btn_link.clicked.connect(self.on_link)
        row.addWidget(self.code_input)
        row.addWidget(self.btn_link)
        layout.addWidget(self.link_row)

        mode_row = QHBoxLayout()
        mode_lbl = QLabel("Metering:")
        mode_lbl.setStyleSheet("color: #a6adc8;")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Off (work only)", "off")
        self.mode_combo.addItem("Basic (compat proxy)", "basic")
        self.mode_combo.addItem("Deep (MITM / Cursor)", "deep")
        self.mode_combo.setStyleSheet(
            "background-color: #2f2e41; color: #cdd6f4; padding: 6px; border-radius: 6px;"
        )
        idx = max(0, self.mode_combo.findData(get_metering_mode()))
        self.mode_combo.setCurrentIndex(idx)
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        mode_row.addWidget(mode_lbl)
        mode_row.addWidget(self.mode_combo, stretch=1)
        layout.addLayout(mode_row)

        self.deep_consent = QCheckBox(
            "I consent to local CA + system proxy while Bridge is ON (restored on Stop)"
        )
        self.deep_consent.setStyleSheet("color: #a6adc8; font-size: 11px;")
        self.deep_consent.setChecked(bool(load_state().get("deepMeteringConsent")))
        self.deep_consent.setVisible(get_metering_mode() == "deep")
        layout.addWidget(self.deep_consent)

        self.btn_toggle = QPushButton("Start Bridge")
        self.btn_toggle.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.btn_toggle.setStyleSheet(
            "background-color: #ca9ee6; color: #11111b; padding: 12px; border-radius: 8px;"
        )
        self.btn_toggle.clicked.connect(self.toggle_bridge)
        layout.addWidget(self.btn_toggle)

        status_frame = QFrame()
        status_frame.setStyleSheet(
            "background-color: #181825; border: 1px solid #414052; border-radius: 10px;"
        )
        status_layout = QVBoxLayout(status_frame)
        status_title = QLabel("STATUS")
        status_title.setStyleSheet("color: #7f849c; font-size: 11px; font-weight: bold;")
        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet("color: #89b4fa; font-size: 13px;")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setMinimumHeight(48)
        status_layout.addWidget(status_title)
        status_layout.addWidget(self.lbl_status)
        layout.addWidget(status_frame)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.tick_timer)
        self._timer.start()

        # If a previous run crashed mid-bridge, put the machine back
        if ROUTE_BACKUP_PATH.exists():
            restore_local_routing()

        self.refresh_link_ui()
        if self.is_linked():
            threading.Thread(target=self.load_profile_async, daemon=True).start()

    def closeEvent(self, event):
        if _bridge_running:
            self.stop_bridge()
        else:
            restore_local_routing()
        super().closeEvent(event)

    def is_linked(self) -> bool:
        return bool(load_state().get("deviceToken"))

    def load_profile_async(self):
        token = load_state().get("deviceToken")
        if not token:
            return
        try:
            profile = fetch_profile(token)
            self._profile = profile
            signals.status_updated.emit(self.lbl_status.text())
            QTimer.singleShot(0, self.render_account)
        except Exception as e:
            signals.status_updated.emit(f"Could not load account: {type(e).__name__}")

    def render_account(self):
        linked = self.is_linked()
        self.btn_unlink.setVisible(linked)
        self.link_label.setVisible(not linked)
        self.link_row.setVisible(not linked)
        if linked and self._profile:
            name = self._profile.get("displayName") or "—"
            email = self._profile.get("email") or ""
            role = self._profile.get("role") or ""
            wallet = self._profile.get("walletAddress") or "(no wallet yet)"
            if len(wallet) > 12:
                wallet = f"{wallet[:4]}…{wallet[-4:]}"
            self.lbl_account.setText(
                f"<b style='color:#a6e3a1'>{name}</b>"
                f"<br/><span style='color:#a6adc8; font-size:12px'>{email} · {role}</span>"
                f"<br/><span style='color:#7f849c; font-size:11px'>Wallet {wallet}</span>"
            )
        elif linked:
            self.lbl_account.setText(
                "<span style='color:#a6e3a1'>Linked</span>"
                "<br/><span style='color:#7f849c; font-size:12px'>Loading account…</span>"
            )
        else:
            self.lbl_account.setText(
                "<span style='color:#a6adc8'>Not linked</span>"
                "<br/><span style='color:#7f849c; font-size:12px'>"
                "Enter a device code from web My Page</span>"
            )

    def refresh_link_ui(self):
        self.render_account()
        if self.is_linked():
            self.lbl_status.setText("Linked. Start Bridge to meter AI usage.")
        else:
            self.lbl_status.setText("Enter a device code from the web My Page to link")

    def on_link(self):
        code = self.code_input.text().strip()
        if not code.isdigit() or len(code) != 6:
            self.lbl_status.setText("Enter a valid 6-digit code")
            return
        try:
            link_device(code)
            self.code_input.clear()
            self.refresh_link_ui()
            self.lbl_status.setText("Link success — loading account…")
            threading.Thread(target=self.load_profile_async, daemon=True).start()
        except Exception as e:
            self.lbl_status.setText(f"Link failed: {type(e).__name__}")

    def on_unlink(self):
        global _bridge_running
        token = load_state().get("deviceToken")
        if _bridge_running:
            self.stop_bridge()
        if token:
            try:
                unlink_device(token)
            except Exception:
                clear_device_token()
                self.lbl_status.setText("Local token cleared")
                self._profile = None
                self._work_started_at = None
                self.refresh_link_ui()
                self.lbl_job.setText("Assignment: —")
                self.lbl_timer.setText("Work time: —")
                return
        else:
            clear_device_token()
        self._profile = None
        self._work_started_at = None
        self.refresh_link_ui()
        self.lbl_job.setText("Assignment: —")
        self.lbl_timer.setText("Work time: —")
        self.lbl_status.setText("Unlinked. Generate a new code to link again.")

    def toggle_bridge(self):
        if _bridge_running:
            self.stop_bridge()
        else:
            self.start_bridge()

    def on_mode_changed(self):
        mode = self.mode_combo.currentData()
        self.deep_consent.setVisible(mode == "deep")
        set_metering_mode_local(mode, consent=self.deep_consent.isChecked())
        token = load_state().get("deviceToken")
        if token:
            try:
                if mode == "deep" and not self.deep_consent.isChecked():
                    self.lbl_status.setText("Deep mode needs consent checkbox before syncing")
                    return
                post_metering_mode(token, mode, deep_consent=self.deep_consent.isChecked())
                self.lbl_status.setText(f"Metering mode → {mode}")
            except Exception as e:
                self.lbl_status.setText(f"Mode sync failed: {type(e).__name__}")

    def _set_bridge_button_running(self, mode: str) -> None:
        self.btn_toggle.setText(f"Stop Bridge ({mode})")
        self.btn_toggle.setStyleSheet(
            "background-color: #f38ba8; color: #11111b; padding: 12px; border-radius: 8px; font-weight: bold;"
        )
        self.btn_toggle.setEnabled(True)

    def _set_bridge_button_idle(self) -> None:
        self.btn_toggle.setText("Start Bridge")
        self.btn_toggle.setStyleSheet(
            "background-color: #ca9ee6; color: #11111b; padding: 12px; border-radius: 8px; font-weight: bold;"
        )
        self.btn_toggle.setEnabled(True)

    def start_bridge(self):
        global _bridge_running, _deep_session
        state = load_state()
        token = state.get("deviceToken")
        if not token:
            self.lbl_status.setText("Link a device code first")
            return

        mode = self.mode_combo.currentData() or get_metering_mode()
        if mode == "deep" and not self.deep_consent.isChecked():
            self.lbl_status.setText("Check Deep consent before Start")
            return

        # Flip the button immediately so the UI never sticks on "Start" while
        # Deep/MITM boot (which can take several seconds) runs off-thread.
        _bridge_running = True
        self._set_bridge_button_running(mode)
        self.btn_toggle.setEnabled(False)
        self.lbl_status.setText(f"Starting {mode}…")
        set_metering_mode_local(mode, consent=self.deep_consent.isChecked())
        consent = self.deep_consent.isChecked()

        def worker():
            global _deep_session
            err: Optional[str] = None
            try:
                if mode != "off":
                    post_metering_mode(token, mode, deep_consent=consent)
                _stop_heartbeat.clear()
                threading.Thread(target=heartbeat_loop, args=(token,), daemon=True).start()
                if mode != "off":
                    threading.Thread(target=run_uvicorn, daemon=True).start()
                    time.sleep(0.6)
                if mode == "basic":
                    apply_local_routing()
                elif mode == "deep":
                    from deep_metering import DeepMeteringSession

                    _deep_session = DeepMeteringSession()
                    _deep_session.start(METER_HOOK_URL)
            except Exception as e:
                err = str(e) or type(e).__name__

            def finish():
                if err:
                    self.lbl_status.setText(f"Start failed: {err}")
                    self.stop_bridge()
                    return
                self.btn_toggle.setEnabled(True)
                if mode == "off":
                    self.lbl_status.setText("Bridge ON · metering off (assignment sync only)")
                elif mode == "basic":
                    self.lbl_status.setText(
                        f"Basic ON · gateway :{GATEWAY_PORT} · API bases redirected"
                    )
                else:
                    self.lbl_status.setText(
                        f"Deep ON · MITM :{os.getenv('MITM_PORT', '8082')} · "
                        "Cursor Agent는 프록시 불가(사용 중단). "
                        "토큰 테스트는 Basic+curl 권장"
                    )

            QTimer.singleShot(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def stop_bridge(self):
        global _bridge_running, _uvicorn_server, _deep_session
        _stop_heartbeat.set()
        if _uvicorn_server is not None:
            _uvicorn_server.should_exit = True
            _uvicorn_server = None
        if _deep_session is not None:
            try:
                _deep_session.stop()
            except Exception:
                pass
            _deep_session = None
        restore_local_routing()
        try:
            from deep_metering import DeepMeteringSession

            DeepMeteringSession().stop()
        except Exception:
            pass
        _bridge_running = False
        self._set_bridge_button_idle()
        self.lbl_status.setText("Bridge stopped · local settings restored")

    def on_assignment(self, data):
        global current_tokens
        if not data:
            self.lbl_job.setText("Assignment: no active job")
            self._work_started_at = None
            return
        title = data.get("title") or data.get("jobId")
        status = data.get("status")
        repo = data.get("githubRepo", "")
        self.lbl_job.setText(f"Assignment: {title}\n{repo} · status={status}")
        self._work_started_at = data.get("workStartedAt")
        remote = int(data.get("totalTokens") or 0)
        if remote > current_tokens:
            current_tokens = remote
            self.update_token_display(current_tokens)

    def tick_timer(self):
        if self._work_started_at:
            self.lbl_timer.setText(f"Work time: {format_elapsed(self._work_started_at)}")
        else:
            self.lbl_timer.setText("Work time: —")

    def update_token_display(self, tokens: int):
        self.lbl_tokens.setText(f"Accumulated Tokens: {tokens:,}")
        self.lbl_usdc.setText(f"Pending Earnings: {(tokens / 1000) * 0.05:.4f} USDC")

    def update_status_bar(self, text: str):
        self.lbl_status.setText(text)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ZkDevPayApp()
    window.show()
    sys.exit(app.exec())
