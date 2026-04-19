import asyncio
import logging
import os
import shutil
import subprocess

from tweety import TwitterAsync

from config import MEDIA_TEMP_DIR

logger = logging.getLogger(__name__)

_app: TwitterAsync | None = None

AUTH_TOKEN_FILE = "auth_token.txt"


def _extract_auth_token_from_cookies(cookies_path: str) -> str | None:
    """Extract auth_token from a Netscape/curl cookie file."""
    if not os.path.exists(cookies_path):
        return None
    with open(cookies_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5] == "auth_token":
                return parts[6]
    return None


async def init_client() -> TwitterAsync:
    """Initialize tweety client with auth token from cookies.

    Priority: session db (auto-saved) → X_cookies.json (Netscape) → env credentials
    """
    global _app

    if _app is not None:
        return _app

    app = TwitterAsync("session")

    session_file = "session.tw_session"
    if os.path.exists(session_file):
        logger.info("Session file found, connecting with saved session...")
        try:
            await app.connect()
            if app.me:
                logger.info("Reconnected as @%s", app.me.username)
                _app = app
                return app
        except Exception as e:
            logger.warning("Saved session failed: %s, trying cookies...", e)

    auth_token = _extract_auth_token_from_cookies("X_cookies.json")
    if auth_token:
        logger.info("Loading auth token from X_cookies.json...")
        try:
            await app.load_auth_token(auth_token)
            logger.info("Logged in as @%s via auth token", app.me.username)
            _app = app
            return app
        except Exception as e:
            logger.error("Auth token login failed: %s", e)
            raise

    raise ValueError(
        "No authentication method available. "
        "Place X_cookies.json (Netscape format) in the project directory."
    )


async def get_new_tweets(screen_name: str, last_tweet_id: str | None) -> list:
    """Fetch tweets from a user, return only those newer than last_tweet_id.

    Returns list of Tweet objects sorted oldest-first (for chronological sending).
    Skips retweets — only original tweets and quotes.
    """
    app = await init_client()

    try:
        tweets = await app.get_tweets(screen_name, pages=1)
    except Exception as e:
        logger.error("Failed to get tweets for @%s: %s", screen_name, e)
        return []

    new_tweets = []
    
    # Flatten tweets (unroll SelfThread if needed)
    flat_tweets = []
    for item in tweets:
        if type(item).__name__ == "SelfThread":
            flat_tweets.extend(getattr(item, "tweets", []))
        else:
            flat_tweets.append(item)

    for tweet in flat_tweets:
        if getattr(tweet, "is_retweet", False):
            continue

        tweet_id = str(getattr(tweet, "id", ""))
        if not tweet_id:
            continue
            
        if last_tweet_id and int(tweet_id) <= int(last_tweet_id):
            continue

        new_tweets.append(tweet)

    new_tweets.sort(key=lambda t: int(str(getattr(t, "id", "0"))))

    if new_tweets:
        logger.info("Found %d new tweet(s) from @%s", len(new_tweets), screen_name)
    else:
        logger.debug("No new tweets from @%s", screen_name)

    return new_tweets


def _ensure_temp_dir() -> str:
    """Ensure media temp directory exists and return its path."""
    os.makedirs(MEDIA_TEMP_DIR, exist_ok=True)
    return MEDIA_TEMP_DIR


async def extract_media(tweet) -> dict:
    """Extract media from a tweet.

    Returns dict with:
        - type: "photo" | "photos" | "video" | "gif" | "none"
        - urls: list of photo URLs (for photos)
        - file_path: local path to downloaded video/gif (for video/gif)
    """
    if not tweet.media:
        return {"type": "none"}

    photos = [m for m in tweet.media if getattr(m, "type", "") == "photo"]
    videos = [m for m in tweet.media if getattr(m, "type", "") == "video"]
    gifs = [m for m in tweet.media if getattr(m, "type", "") == "animated_gif"]

    if videos:
        return await _extract_video(videos[0], tweet)

    if gifs:
        return await _extract_video(gifs[0], tweet)

    if photos:
        urls = []
        for p in photos:
            url = getattr(p, "media_url_https", None) or getattr(p, "media_url", None)
            if url:
                urls.append(url)
        if len(urls) == 1:
            return {"type": "photo", "urls": urls}
        elif urls:
            return {"type": "photos", "urls": urls}

    return {"type": "none"}


async def _extract_video(media_obj, tweet) -> dict:
    """Download video from tweet using tweety streams or yt-dlp fallback."""
    temp_dir = _ensure_temp_dir()
    file_path = os.path.join(temp_dir, f"video_{tweet.id}.mp4")

    try:
        best = getattr(media_obj, "best_stream", None)
        streams = getattr(media_obj, "streams", None)

        download_url = None
        if best:
            download_url = getattr(best, "url", None)
        elif streams:
            best_stream = max(streams, key=lambda s: getattr(s, "bitrate", 0) or 0)
            download_url = getattr(best_stream, "url", None)

        if download_url:
            if "m3u8" in download_url:
                return await _download_with_ytdlp(tweet, file_path)

            import httpx
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.get(download_url)
                resp.raise_for_status()
                with open(file_path, "wb") as f:
                    f.write(resp.content)

            file_size = os.path.getsize(file_path)
            if file_size > 50 * 1024 * 1024:
                logger.warning("Video too large (%d MB), using yt-dlp", file_size // (1024 * 1024))
                os.remove(file_path)
                return await _download_with_ytdlp(tweet, file_path)

            logger.info("Downloaded video via tweety: %s", file_path)
            return {"type": "video", "file_path": file_path}

    except Exception as e:
        logger.warning("Tweety video download failed, trying yt-dlp: %s", e)

    return await _download_with_ytdlp(tweet, file_path)


async def _download_with_ytdlp(tweet, file_path: str) -> dict:
    """Fallback: download video using yt-dlp."""
    tweet_url = f"https://x.com/i/status/{tweet.id}"

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "yt-dlp",
                "--no-warnings",
                "-f", "best[filesize<50M]/best",
                "-o", file_path,
                "--no-playlist",
                tweet_url,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode == 0 and os.path.exists(file_path):
            logger.info("Downloaded video via yt-dlp: %s", file_path)
            return {"type": "video", "file_path": file_path}
        else:
            logger.error("yt-dlp failed: %s", result.stderr)
    except Exception as e:
        logger.error("yt-dlp error: %s", e)

    return {"type": "none"}


def cleanup_media() -> None:
    """Remove all files in the temp media directory."""
    if os.path.exists(MEDIA_TEMP_DIR):
        shutil.rmtree(MEDIA_TEMP_DIR, ignore_errors=True)
        logger.debug("Cleaned up media temp directory")
