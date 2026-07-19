# zk-DevPay Local Dashboard (Module 1) ⚡

> AI contribution-based real-time token tracking & End-to-End Encrypted (E2EE) desktop gateway.

This repository contains **Module 1 (Off-Chain Developer Activity)** of the zk-DevPay system. It functions as a complete local UI desktop application that intercepts Google Gemini API calls via a background gateway without disrupting the developer's default CLI or IDE workflow. It securely parses token usage in real time and encrypts prompt payloads with asymmetric encryption (E2EE) before relaying them to the verification cloud server.

---

## 🏗️ Architecture Blueprint

<img width="1024" height="559" alt="Image" src="https://github.com/user-attachments/assets/d11a3489-b9a7-424b-b82c-ba0f109325f5" />

---

## ✨ Core Features

* **Zero-UX LiteLLM Proxy Gate:** Intercepts incoming Google Gemini API traffic on port `4000` seamlessly behind the scenes, leaving developer workflows entirely untouched.
* **End-to-End Encryption (E2EE):** Leverages an RSA public key retrieved from Module 2 (GCP Verification Server) to securely lock prompt content directly on the local machine before transmission.
* **Real-Time Dashboard UI:** Built with a clean, dark-themed `PyQt6` GUI to visually stream accumulated token counts and track `Pending USDC` rewards live during development.
* **Isolated Key Infrastructure:** The developer's private key remains decoupled from the local software client. The app maps settlement routes using strictly the developer's public wallet address.

---

## 🛠️ Quick Start

### 1. Configure Environment Variables (`.env`)
Create a `.env` file in the root directory and register your production Gemini API key alongside your Solana Devnet address.

GEMINI_API_KEY=AIzaSyYourActualRealGeminiApiKeyHere...
DEVELOPER_WALLET=YourSolanaDevnetWalletAddressHere...

### 2. Install Project Dependencies
Activate your Python virtual environment (`venv`) and install the required modules.

# Activate virtual environment (Mac/Linux)
$ source venv/bin/activate

# Install core packages
$ pip install litellm cryptography requests PyQt6 uvicorn

### 3. Run the Desktop Application
$ python app_ui.py

* Once the dashboard pops up, click the **`[Start Encryption Bridge]`** button to activate the port `4000` gateway wrapper.

---

## 🧪 Integration Testing Protocol

While keeping the local UI dashboard active, open a **separate terminal window** and run the following commands to test traffic interception:

# 1. Reroute your AI CLI endpoint to point directly at the local gateway proxy
$ export GEMINI_API_BASE="http://localhost:4000/v1"

# 2. Fire a mock conversational completion payload (curl test)
$ curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "Hello zk-DevPay"}]}'

> **Verification Check:** The instant the payload clears the proxy, the `Accumulated Tokens` field on your desktop GUI will dynamically increment, and the bottom status bar will pivot to reflect active secure routing.

---

## 📦 Production Native Build

To bundle the Python dependencies and UI configurations into a standalone, single-click executable binary (`.app` or `.exe`), compile the project via `PyInstaller`.

# Package a standalone application for macOS
$ pyinstaller --noconfirm --onedir --windowed --name="zk-DevPay" app_ui.py

* The absolute standalone binary package will be generated inside the newly created `dist/zk-DevPay.app` directory.
