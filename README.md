# zk-DevPay Local Dashboard (Module 1)

PyQt6 desktop bridge: job/assignment status sync, optional OpenAI-compatible
metering gateway. **API keys are never collected** by this app.

Repo: https://github.com/mikohatsu/zk-devpay-client  
Works with: [zk-devpay-server](https://github.com/mikohatsu/zk-devpay-server) (web BFF + core).

## Metering modes

| Mode | Behavior |
|------|----------|
| **Off** | Assignment / work-time sync only. Jobs can pay **work budget** without tokens. |
| **Basic** | Loopback OpenAI-compatible proxy. Tokens + E2EE prompts when tools hit the gateway. |
| **Deep** | Optional MITM (experimental). **Do not use for Cursor Agent** — proxy breaks generation. |

See `docs/LOCAL_METERING_DESIGN.md` in zk-devpay-server.

## Local development

### Prerequisites

Bring up the server stack first (emulator + core `:8080` + web BFF `:8081` + SPA `:5173`).
Step-by-step: [zk-devpay-server README — Local development](https://github.com/mikohatsu/zk-devpay-server#local-development-full-stack).

### Config (`.env`)

Service URLs only — **do not** put `*_API_KEY` here:

```env
WEB_BFF_URL=http://localhost:8081
CORE_API_URL=http://localhost:8080
GATEWAY_PORT=4001
MITM_PORT=8082
```

### Install & run

```bash
cd zk-devpay-client
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
# source venv/bin/activate

pip install litellm cryptography requests PyQt6 uvicorn fastapi
# Deep only (Python 3.9):
# pip install "mitmproxy==9.0.1"

python app_ui.py
```

### Use with the web app

1. Open http://localhost:5173 → developer account → **My Page** → device code.
2. Enter the code in the client → **Link**.
3. Accept a job on the web → **Start work**.
4. Client: metering **Off** for status-only, or **Basic** + **Start Bridge** for token tests.
5. Basic token smoke test (with your own provider key in the shell, not in this app):

```powershell
$base = "http://127.0.0.1:4001"
$body = @{
  model = "gpt-4o-mini"
  messages = @(@{ role = "user"; content = "ping" })
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri "$base/v1/chat/completions" -Method POST `
  -ContentType "application/json" `
  -Headers @{ Authorization = "Bearer $env:OPENAI_API_KEY" } `
  -Body $body
```

Encrypted prompts → `POST {CORE_API_URL}/api/session/log`  
Usage heartbeats → `POST {WEB_BFF_URL}/api/client/usage` (basic/deep only)
