import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "")

# Tổng hợp tất cả chat IDs cần gửi (cá nhân + group)
TELEGRAM_CHAT_IDS: list[str] = [
    cid for cid in [TELEGRAM_CHAT_ID, TELEGRAM_GROUP_ID] if cid.strip()
]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))

# X/Twitter accounts
MONITORED_ACCOUNTS = [
    acc.strip()
    for acc in os.getenv("MONITORED_ACCOUNTS", "").split(",")
    if acc.strip()
]

# Facebook fanpages
FB_PAGES = [
    url.strip()
    for url in os.getenv("FB_PAGES", "").split(",")
    if url.strip()
]

FB_USERNAME = os.getenv("FB_USERNAME", "")
FB_PASSWORD = os.getenv("FB_PASSWORD", "")

STORAGE_FILE = "storage.json"
MEDIA_TEMP_DIR = "media_temp"
FB_COOKIES_FILE = "fb_cookies.json"
