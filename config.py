import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))

MONITORED_ACCOUNTS = [
    acc.strip()
    for acc in os.getenv("MONITORED_ACCOUNTS", "").split(",")
    if acc.strip()
]

STORAGE_FILE = "storage.json"
MEDIA_TEMP_DIR = "media_temp"
