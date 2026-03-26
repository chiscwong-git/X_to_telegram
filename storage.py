import json
import logging
import os

from config import STORAGE_FILE

logger = logging.getLogger(__name__)


def load_state() -> dict[str, str]:
    """Load last-seen tweet IDs from storage file.
    Returns dict mapping screen_name -> last_tweet_id.
    """
    if not os.path.exists(STORAGE_FILE):
        return {}
    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Failed to load storage file, starting fresh: %s", e)
        return {}


def save_state(state: dict[str, str]) -> None:
    """Persist state to storage file."""
    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def is_new_tweet(last_tweet_id: str | None, tweet_id: str) -> bool:
    """Check if tweet_id is newer than last_tweet_id.
    Tweet IDs are snowflake-based — larger ID = newer tweet.
    """
    if not last_tweet_id:
        return True
    return int(tweet_id) > int(last_tweet_id)


def update_last_tweet(state: dict[str, str], screen_name: str, tweet_id: str) -> None:
    """Update the last-seen tweet ID for the given account if it's newer."""
    current = state.get(screen_name)
    if not current or int(tweet_id) > int(current):
        state[screen_name] = tweet_id
