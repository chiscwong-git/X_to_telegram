import asyncio
import logging
import sys

from config import CHECK_INTERVAL, MONITORED_ACCOUNTS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from storage import is_new_tweet, load_state, save_state, update_last_tweet
from telegram_sender import send_tweet
from twitter_scraper import cleanup_media, extract_media, get_new_tweets, init_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("x_monitor")


def validate_config() -> bool:
    """Validate required configuration is present."""
    errors = []

    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is not set")
    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID is not set")
    if not MONITORED_ACCOUNTS:
        errors.append("MONITORED_ACCOUNTS is empty")

    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        return False
    return True


async def process_account(screen_name: str, state: dict) -> bool:
    """Process a single account: fetch new tweets and send to Telegram.

    Returns True if state was updated (new tweets found).
    """
    last_id = state.get(screen_name)
    state_changed = False

    try:
        new_tweets = await get_new_tweets(screen_name, last_id)

        if not last_id and new_tweets:
            latest = new_tweets[-1]
            update_last_tweet(state, screen_name, str(latest.id))
            logger.info(
                "First run for @%s — saved latest tweet ID %s, skipping send",
                screen_name,
                latest.id,
            )
            return True

        for tweet in new_tweets:
            media_info = await extract_media(tweet)
            sent = await send_tweet(tweet, screen_name, media_info)

            if sent:
                update_last_tweet(state, screen_name, str(tweet.id))
                state_changed = True

            await asyncio.sleep(2)

    except Exception as e:
        logger.error("Error processing @%s: %s", screen_name, e)

    return state_changed


async def run_cycle(state: dict) -> dict:
    """Run one monitoring cycle across all accounts."""
    state_changed = False

    for screen_name in MONITORED_ACCOUNTS:
        logger.info("Checking @%s...", screen_name)

        changed = await process_account(screen_name, state)
        if changed:
            state_changed = True

        await asyncio.sleep(3)

    if state_changed:
        save_state(state)

    cleanup_media()
    return state


async def main() -> None:
    """Main entry point: init client and run monitoring loop."""
    logger.info("=" * 50)
    logger.info("X Twitter Monitor Bot Starting")
    logger.info("=" * 50)

    if not validate_config():
        logger.error("Invalid configuration. Exiting.")
        sys.exit(1)

    logger.info("Monitoring accounts: %s", ", ".join(f"@{a}" for a in MONITORED_ACCOUNTS))
    logger.info("Check interval: %d seconds (%d minutes)", CHECK_INTERVAL, CHECK_INTERVAL // 60)

    try:
        await init_client()
        logger.info("Twitter client initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize Twitter client: %s", e)
        sys.exit(1)

    state = load_state()
    logger.info("Loaded state for %d account(s)", len(state))

    cycle_count = 0
    while True:
        cycle_count += 1
        logger.info("--- Cycle #%d ---", cycle_count)

        try:
            state = await run_cycle(state)
        except Exception as e:
            logger.error("Cycle #%d failed: %s", cycle_count, e)

        logger.info("Next check in %d seconds...", CHECK_INTERVAL)
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
