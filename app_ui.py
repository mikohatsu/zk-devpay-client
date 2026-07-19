# app_ui.py
import sys
import os
import threading
import base64
import requests
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QLabel, QPushButton, QLineEdit, QFrame)
from PyQt6.QtCore import pyqtSignal, QObject
from PyQt6.QtGui import QFont
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization, hashes
import litellm
import uvicorn

# .env 파일이 있으면 환경변수로 로드 (간이 구현)
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ[k] = v

BACKEND_URL = "http://localhost:8000"
DEVELOPER_WALLET = os.getenv("DEVELOPER_WALLET", "NotConfigured")

class BridgeSignals(QObject):
    token_updated = pyqtSignal(int)
    status_updated = pyqtSignal(str)

signals = BridgeSignals()
current_tokens = 0

def encrypted_log_handler(model_call_dict):
    global current_tokens
    try:
        response_data = model_call_dict["response_obj"]
        raw_prompt = model_call_dict["additional_args"]["complete_input_instructions"]
        total_tokens = response_data["usage"]["total_tokens"]
        
        current_tokens += total_tokens
        signals.token_updated.emit(current_tokens)
        signals.status_updated.emit("🔒 기여 감지! 비대칭 암호화 진행 중...")

        # 1. GCP 서버에서 공개 자물쇠(Public Key) 가져오기
        key_response = requests.get(f"{BACKEND_URL}/v1/public-key", timeout=5)
        key_response.raise_for_status()
        public_key = serialization.load_pem_public_key(key_response.content)

        # 2. 공개키로 프롬프트 평문 잠그기
        encrypted_prompt = public_key.encrypt(
            raw_prompt.encode(),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )

        # 3. 암호문 패킷 전송
        payload = {
            "developer_wallet": DEVELOPER_WALLET,
            "encrypted_prompt": base64.b64encode(encrypted_prompt).decode('utf-8'),
            "total_tokens": total_tokens,
            "model": model_call_dict["model"]
        }
        requests.post(f"{BACKEND_URL}/v1/secure-verify", json=payload, timeout=5)
        signals.status_updated.emit("✅ 암호화 패킷 전송 완료! 온체인 정산 대기 중.")

    except Exception as e:
        signals.status_updated.emit(f"❌ 오류: {e}")

litellm.success_callback = [encrypted_log_handler]

class ZkDevPayApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("zk-DevPay Local Dashboard")
        self.setFixedSize(450, 480)
        self.setStyleSheet("background-color: #1e1e2e; color: #cdd6f4;")
        
        # Signals mapping
        signals.token_updated.connect(self.update_token_display)
        signals.status_updated.connect(self.update_status_bar)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(15)

        title = QLabel("zk-DevPay ⚡")
        title.setFont(QFont("Arial", 22, QFont.Weight.Bold))
        title.setStyleSheet("color: #ca9ee6;")
        layout.addWidget(title)

        card = QFrame()
        card.setStyleSheet("background-color: #252434; border-radius: 12px;")
        card_layout = QVBoxLayout(card)
        self.lbl_tokens = QLabel("Accumulated Tokens: 0")
        self.lbl_tokens.setFont(QFont("Arial", 14))
        self.lbl_usdc = QLabel("Pending Earnings: 0.0000 USDC")
        self.lbl_usdc.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        self.lbl_usdc.setStyleSheet("color: #a6e3a1;")
        card_layout.addWidget(self.lbl_tokens)
        card_layout.addWidget(self.lbl_usdc)
        layout.addWidget(card)

        layout.addWidget(QLabel("Registered Wallet Address:"))
        self.wallet_input = QLineEdit(DEVELOPER_WALLET)
        self.wallet_input.setReadOnly(True)
        self.wallet_input.setStyleSheet("background-color: #2f2e41; padding: 8px; border-radius: 6px; color: #a6adc8;")
        layout.addWidget(self.wallet_input)

        self.btn_toggle = QPushButton("Start Encryption Bridge")
        self.btn_toggle.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.btn_toggle.setStyleSheet("background-color: #ca9ee6; color: #11111b; padding: 12px; border-radius: 8px;")
        self.btn_toggle.clicked.connect(self.start_bridge)
        layout.addWidget(self.btn_toggle)

        self.lbl_status = QLabel("🟢 System Ready")
        self.lbl_status.setStyleSheet("color: #89b4fa;")
        layout.addWidget(self.lbl_status)

    def start_bridge(self):
        self.btn_toggle.setEnabled(False)
        self.btn_toggle.setText("Bridge Running (Port 4000)")
        self.btn_toggle.setStyleSheet("background-color: #414052; color: #7f849c; padding: 12px; border-radius: 8px;")
        
        t = threading.Thread(target=lambda: uvicorn.run(litellm.app, host="127.0.0.1", port=4000), daemon=True)
        t.start()
        self.lbl_status.setText("⚡ 로컬 게이트웨이 활성화 완료 (Port 4000)")

    def update_token_display(self, tokens):
        self.lbl_tokens.setText(f"Accumulated Tokens: {tokens:,}")
        self.lbl_usdc.setText(f"Pending Earnings: {(tokens / 1000) * 0.05:.4f} USDC")

    def update_status_bar(self, text):
        self.lbl_status.setText(text)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ZkDevPayApp()
    window.show()
    sys.exit(app.exec())