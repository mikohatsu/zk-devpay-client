# zk-DevPay Local Dashboard (Module 1)

PyQt6 desktop bridge with **opt-in** AI metering. API keys are never collected.

## Metering modes

| Mode | Behavior |
|------|----------|
| **Off** | Assignment sync only. No proxy / no MITM. Jobs pay **work budget** only. |
| **Basic** | OpenAI-compatible loopback proxy + API-base redirect. Tokens + E2EE prompts. |
| **Deep** | Consent required. Optional MITM for tools that honor a local proxy. **Cursor Agent chat cannot be metered this way** (proxy breaks generation). Prefer Basic + OpenAI-compatible clients for token tests. |

Prompts (when metered) are encrypted to the session public key and used **only for AI
verification**, then purged. Employers never see them.

See `docs/LOCAL_METERING_DESIGN.md` in zk-devpay-server.

## Config (`.env`)

Service URLs only — **do not** put `*_API_KEY` here:

```
WEB_BFF_URL=http://localhost:8081
CORE_API_URL=http://localhost:8080
GATEWAY_PORT=4001
MITM_PORT=8082
```

## Run

```bash
pip install litellm cryptography requests PyQt6 uvicorn fastapi
# Deep mode (Python 3.9: mitmproxy 9.x)
pip install "mitmproxy==9.0.1"
python app_ui.py
```

1. Web **My Page** → set metering mode (and Deep consent if needed) → device code → **Link**.
2. Accept a job, **Start work**, then **Start Bridge**.
3. **Stop Bridge** restores proxy / CA / API-base settings.

Encrypted prompts → `POST {CORE_API_URL}/api/session/log`  
Usage heartbeats → `POST {WEB_BFF_URL}/api/client/usage` (basic/deep only)
