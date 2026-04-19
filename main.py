import asyncio
import logging
import os
import sys

from config import (
    CHECK_INTERVAL,
    FB_PAGES,
    MONITORED_ACCOUNTS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from facebook_scraper import (
    close_fb_client,
    download_fb_video,
    get_new_posts,
    init_fb_client,
)
from storage import load_state, save_state, update_last_tweet
from telegram_sender import send_fb_post, send_tweet
from twitter_scraper import cleanup_media, extract_media, get_new_tweets, init_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("monitor_bot")

LOCK_FILE = "bot.lock"


def _acquire_lock() -> bool:
    """Prevent multiple bot instances from running simultaneously."""
    if os.path.exists(LOCK_FILE):
        try:
            existing_pid = int(open(LOCK_FILE).read().strip())
            # Check if the process with that PID is still alive
            import psutil
            if psutil.pid_exists(existing_pid):
                logger.error(
                    "Another instance is already running (PID %d). "
                    "Stop it first or delete '%s'.",
                    existing_pid, LOCK_FILE,
                )
                return False
            # Stale lock file — process is dead
            logger.warning("Removing stale lock file (PID %d no longer running)", existing_pid)
        except Exception:
            pass  # Corrupt lock file — overwrite it

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass



def validate_config() -> bool:
    """Validate required configuration is present."""
    errors = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is not set")
    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID is not set")
    if not MONITORED_ACCOUNTS and not FB_PAGES:
        errors.append("No sources configured (MONITORED_ACCOUNTS and FB_PAGES are both empty)")
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        return False
    return True


# ─────────────────────────── X / Twitter ────────────────────────────

async def process_x_account(screen_name: str, state: dict) -> bool:
    """Process a single X account: fetch new tweets and send to Telegram."""
    last_id = state.get(screen_name)
    state_changed = False

    try:
        new_tweets = await get_new_tweets(screen_name, last_id)

        if not last_id and new_tweets:
            latest = new_tweets[-1]
            update_last_tweet(state, screen_name, str(latest.id))
            logger.info("First run @%s — saved tweet ID %s, skipping send", screen_name, latest.id)
            return True

        for tweet in new_tweets:
            media_info = await extract_media(tweet)
            sent = await send_tweet(tweet, screen_name, media_info)
            if sent:
                update_last_tweet(state, screen_name, str(tweet.id))
                state_changed = True
            await asyncio.sleep(2)

    except Exception as e:
        logger.error("Error processing X @%s: %s", screen_name, e)

    return state_changed


# ─────────────────────────── Facebook ───────────────────────────────

def _fb_state_key(page_url: str) -> str:
    """Derive storage key for a Facebook page URL."""
    from urllib.parse import urlparse
    path = urlparse(page_url).path.strip("/").split("/")[0]
    return f"fb_{path.lower()}"


async def process_fb_page(page_url: str, state: dict) -> bool:
    """Process a single FB fanpage: fetch new posts and send to Telegram."""
    key = _fb_state_key(page_url)
    last_id = state.get(key)
    state_changed = False

    try:
        posts = await get_new_posts(page_url, last_id)

        # First run: just save the latest post ID, don't send
        if last_id is None:
            if posts:
                latest = posts[0]  # posts returned newest-first on first run
                state[key] = latest["id"]
                logger.info(
                    "First run %s — saved post ID %s, skipping send", key, latest["id"]
                )
                return True
            return False

        for post in posts:
            video_path = None

            # Download video if detected
            if post.get("has_video") and post.get("post_url"):
                logger.info("Downloading FB video from %s...", post["post_url"])
                video_path = await download_fb_video(post["post_url"])

            sent = await send_fb_post(post, video_file_path=video_path)
            if sent:
                state[key] = post["id"]
                state_changed = True
            await asyncio.sleep(3)

    except Exception as e:
        logger.error("Error processing FB page %s: %s", page_url, e)

    return state_changed


# ─────────────────────────── Main loop ──────────────────────────────

async def run_cycle(state: dict) -> dict:
    """Run one full monitoring cycle: X accounts then FB pages."""
    state_changed = False

    # X/Twitter accounts
    for screen_name in MONITORED_ACCOUNTS:
        logger.info("Checking X @%s...", screen_name)
        changed = await process_x_account(screen_name, state)
        if changed:
            state_changed = True
        await asyncio.sleep(3)

    # Facebook fanpages
    for page_url in FB_PAGES:
        logger.info("Checking FB %s...", page_url)
        changed = await process_fb_page(page_url, state)
        if changed:
            state_changed = True
        await asyncio.sleep(3)

    if state_changed:
        save_state(state)

    cleanup_media()
    return state


async def main() -> None:
    """Main entry point."""
    if not _acquire_lock():
        sys.exit(1)

    try:
        logger.info("=" * 55)
        logger.info("  X + Facebook → Telegram Monitor Bot")
        logger.info("=" * 55)

        if not validate_config():
            sys.exit(1)

        if MONITORED_ACCOUNTS:
            logger.info("X accounts : %s", ", ".join(f"@{a}" for a in MONITORED_ACCOUNTS))
        if FB_PAGES:
            logger.info("FB pages   : %s", ", ".join(FB_PAGES))
        logger.info("Interval   : %ds (%d min)", CHECK_INTERVAL, CHECK_INTERVAL // 60)

        # Init X client
        if MONITORED_ACCOUNTS:
            try:
                await init_client()
                logger.info("✓ X/Twitter client ready")
            except Exception as e:
                logger.error("Failed to init X client: %s", e)
                if not FB_PAGES:
                    sys.exit(1)

        # Init Facebook client
        if FB_PAGES:
            ok = await init_fb_client()
            if ok:
                logger.info("✓ Facebook client ready")
            else:
                logger.error("Failed to init Facebook client — FB pages will be skipped")

        state = load_state()
        logger.info("Loaded state for %d source(s)", len(state))

        cycle_count = 0
        run_once = os.environ.get("RUN_ONCE") == "1"
        
        while True:
            cycle_count += 1
            logger.info("─── Cycle #%d ───", cycle_count)

            try:
                state = await run_cycle(state)
            except Exception as e:
                logger.error("Cycle #%d failed: %s", cycle_count, e)

            if run_once:
                logger.info("RUN_ONCE is set. Exiting after 1 cycle.")
                break

            logger.info("Next check in %ds...", CHECK_INTERVAL)
            await asyncio.sleep(CHECK_INTERVAL)

    finally:
        _release_lock()
        await close_fb_client()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")

