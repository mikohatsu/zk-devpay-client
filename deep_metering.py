# deep_metering.py — opt-in Deep mode (local CA + mitmproxy).
#
# Only AI-host allowlist traffic is parsed for tokens. API keys are never written
# to disk or status logs. Stop / crash recovery restores CA install + backups.
#
# IMPORTANT product limit: forcing Cursor Agent through this proxy breaks
# streaming. We deliberately do NOT rewrite Cursor settings.json anymore
# (_patch_cursor_proxy is a no-op). Prefer Basic gateway for real token tests.
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

DEEP_DIR = Path(os.getenv("ZK_DEVPAY_DEEP_DIR", ".zk-devpay-deep"))
BACKUP_PATH = Path(os.getenv("ZK_DEVPAY_DEEP_BACKUP", ".zk-devpay-deep-backup.json"))
MITM_PORT = int(os.getenv("MITM_PORT", "8082"))

# Hosts we decrypt. Everything else is left untouched by the addon parser.
AI_HOST_SUFFIXES = (
    "openai.com",
    "api.openai.com",
    "anthropic.com",
    "api.anthropic.com",
    "googleapis.com",
    "generativelanguage.googleapis.com",
    "cursor.sh",
    "api2.cursor.sh",
    "api3.cursor.sh",
    "api4.cursor.sh",
    "api5.cursor.sh",
    "api.cursor.sh",
    "cursorapi.com",
    "cursor-cdn.com",
    "openrouter.ai",
)


def is_ai_host(host: str) -> bool:
    h = (host or "").lower().split(":")[0]
    return any(h == s or h.endswith("." + s) for s in AI_HOST_SUFFIXES)


def _ensure_dirs() -> None:
    DEEP_DIR.mkdir(parents=True, exist_ok=True)


def _win_install_ca(cert_path: Path) -> bool:
    if sys.platform != "win32" or not cert_path.exists():
        return False
    try:
        subprocess.run(
            ["certutil", "-user", "-addstore", "Root", str(cert_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except Exception:
        return False


def _win_uninstall_ca() -> None:
    if sys.platform != "win32":
        return
    # mitmproxy default CN
    for cn in ("mitmproxy", "zk-DevPay Local Metering CA"):
        try:
            subprocess.run(
                ["certutil", "-user", "-delstore", "Root", cn],
                capture_output=True,
                text=True,
            )
        except Exception:
            pass


def _win_proxy_get() -> dict[str, Any]:
    import winreg

    out: dict[str, Any] = {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": ""}
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            for name in ("ProxyEnable", "ProxyServer", "ProxyOverride"):
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                    out[name] = val
                except FileNotFoundError:
                    pass
    except Exception:
        pass
    return out


def _win_proxy_set(enable: int, server: str, override: str) -> None:
    import winreg
    import ctypes

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(enable))
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
        winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, override)
    try:
        internet_set_option = ctypes.windll.Wininet.InternetSetOptionW
        internet_set_option(0, 39, 0, 0)
        internet_set_option(0, 37, 0, 0)
    except Exception:
        pass


def _cursor_settings_path() -> Optional[Path]:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "Cursor" / "User" / "settings.json"
    home = Path.home()
    for p in (
        home / "Library" / "Application Support" / "Cursor" / "User" / "settings.json",
        home / ".config" / "Cursor" / "User" / "settings.json",
    ):
        if p.exists():
            return p
    return None


def _patch_cursor_proxy(mitm_port: int) -> Optional[dict[str, Any]]:
    """Disabled: forcing Cursor through MITM breaks Agent streaming entirely.

    Cursor's always-local / HTTP2 agent path does not work reliably via
    mitmproxy. Deep mode must not rewrite Cursor settings.json.
    """
    return None


def _restore_cursor_proxy(backup: Optional[dict[str, Any]]) -> None:
    if not backup:
        return
    path = Path(backup.get("path", ""))
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    for key, old in (backup.get("keys") or {}).items():
        if old is None:
            data.pop(key, None)
        else:
            data[key] = old
    try:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def write_addon_script(meter_hook_url: str) -> Path:
    """Emit a mitmproxy addon that posts metering events to the local bridge."""
    _ensure_dirs()
    path = DEEP_DIR / "meter_addon.py"
    script = '''\
"""zk-DevPay mitmproxy addon — meter allowlisted AI HTTPS only."""
import json
import urllib.request

from mitmproxy import http

AI_SUFFIXES = %s
METER_HOOK = %r


def _ai_host(host: str) -> bool:
    h = (host or "").lower().split(":")[0]
    return any(h == s or h.endswith("." + s) for s in AI_SUFFIXES)


def _post_meter(prompt: str, tokens: int) -> None:
    payload = json.dumps({"prompt": prompt[:200_000], "totalTokens": int(tokens)}).encode()
    req = urllib.request.Request(
        METER_HOOK,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def _usage_tokens(obj) -> int:
    if not isinstance(obj, dict):
        return 0
    usage = obj.get("usage") or {}
    total = int(usage.get("total_tokens") or 0)
    if total:
        return total
    return int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)


def _prompt_from_request(raw: bytes) -> str:
    try:
        body = json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    msgs = body.get("messages") or body.get("input") or []
    parts = []
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict):
                c = m.get("content", "")
                if isinstance(c, list):
                    c = " ".join(
                        str(p.get("text", "")) for p in c if isinstance(p, dict)
                    )
                parts.append("%%s: %%s" %% (m.get("role", "user"), c))
    elif isinstance(msgs, str):
        parts.append(msgs)
    prompt = body.get("prompt")
    if prompt:
        parts.append(str(prompt))
    return "\\n".join(parts)


class ZkDevPayMeter:
    def response(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        if not _ai_host(host):
            return
        prompt = _prompt_from_request(flow.request.content or b"")
        tokens = 0
        raw = flow.response.content or b""
        text = raw.decode("utf-8", errors="ignore")
        try:
            data = json.loads(text)
            tokens = _usage_tokens(data)
        except Exception:
            for line in text.splitlines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    continue
                try:
                    data = json.loads(chunk)
                    t = _usage_tokens(data)
                    if t:
                        tokens = t
                except Exception:
                    pass
        if tokens <= 0 and prompt:
            tokens = max(1, len(prompt) // 4)
        if prompt or tokens:
            _post_meter(prompt or "(no prompt text)", tokens)


addons = [ZkDevPayMeter()]
''' % (repr(AI_HOST_SUFFIXES), meter_hook_url)
    path.write_text(script, encoding="utf-8")
    return path


def _mitm_ca_cert_path() -> Path:
    # mitmproxy writes this under confdir after first launch
    return DEEP_DIR / "mitmproxy-ca-cert.pem"


class DeepMeteringSession:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._ca_installed = False

    def start(self, meter_hook_url: str) -> None:
        if BACKUP_PATH.exists():
            self.stop()

        _ensure_dirs()
        addon = write_addon_script(meter_hook_url)
        confdir = str(DEEP_DIR.resolve())

        backup: dict[str, Any] = {
            "proxy": _win_proxy_get() if sys.platform == "win32" else {},
            "caInstalled": False,
            "mitmPort": MITM_PORT,
            "cursorSettings": None,
        }

        # 1) Start mitmdump first so it creates its own CA in confdir
        mitmdump_exe = Path(sys.executable).with_name("mitmdump.exe")
        if not mitmdump_exe.exists():
            mitmdump_exe = Path(sys.executable).with_name("mitmdump")
        attempts = [
            [
                str(mitmdump_exe),
                "-p",
                str(MITM_PORT),
                "-s",
                str(addon.resolve()),
                "--set",
                f"confdir={confdir}",
                "--set",
                "http2=false",
                "-q",
            ],
            [
                sys.executable,
                "-c",
                (
                    "from mitmproxy.tools.main import mitmdump; import sys; "
                    f"sys.argv = ['mitmdump', '-p', '{MITM_PORT}', '-s', r'{addon.resolve()}', "
                    f"'--set', 'confdir={confdir}', '--set', 'http2=false', '-q']; mitmdump()"
                ),
            ],
        ]
        last_err: Optional[Exception] = None
        log_path = DEEP_DIR / "mitmdump.log"
        for cmd in attempts:
            try:
                with open(log_path, "w", encoding="utf-8") as logf:
                    self._proc = subprocess.Popen(
                        cmd,
                        stdout=logf,
                        stderr=subprocess.STDOUT,
                    )
                # Wait until listening / CA appears
                for _ in range(20):
                    time.sleep(0.25)
                    if self._proc.poll() is not None:
                        break
                    if _mitm_ca_cert_path().exists():
                        break
                if self._proc.poll() is None:
                    last_err = None
                    break
                err_tail = ""
                try:
                    err_tail = log_path.read_text(encoding="utf-8")[-500:]
                except Exception:
                    pass
                last_err = RuntimeError(
                    f"exited early: {' '.join(cmd)} :: {err_tail}"
                )
            except Exception as e:
                last_err = e
                self._proc = None

        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError(
                "mitmproxy failed to start — run: pip install mitmproxy "
                f"({last_err})"
            )

        # 2) Trust the CA mitmproxy actually uses (not a separate custom CA)
        ca_path = _mitm_ca_cert_path()
        if sys.platform == "win32" and ca_path.exists():
            installed = _win_install_ca(ca_path)
            backup["caInstalled"] = installed
            self._ca_installed = installed
            # Do NOT enable WinINET system proxy — it breaks unrelated apps and
            # Cursor Agent often ignores it anyway. Cursor is patched below.

        # Cursor must go through MITM explicitly (http.proxy). System proxy alone
        # is unreliable and HTTP/2 streaming breaks without disableHttp2.
        backup["cursorSettings"] = _patch_cursor_proxy(MITM_PORT)

        BACKUP_PATH.write_text(json.dumps(backup, indent=2), encoding="utf-8")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None

        if not BACKUP_PATH.exists():
            return
        try:
            backup = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
        except Exception:
            BACKUP_PATH.unlink(missing_ok=True)
            return

        _restore_cursor_proxy(backup.get("cursorSettings"))

        if sys.platform == "win32":
            prev = backup.get("proxy") or {}
            _win_proxy_set(
                int(prev.get("ProxyEnable") or 0),
                str(prev.get("ProxyServer") or ""),
                str(prev.get("ProxyOverride") or ""),
            )
            if backup.get("caInstalled"):
                _win_uninstall_ca()

        try:
            BACKUP_PATH.unlink(missing_ok=True)
        except Exception:
            pass
