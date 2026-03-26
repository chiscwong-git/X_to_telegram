import logging
import os

from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode

from config import MEDIA_TEMP_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_bot: Bot | None = None


def _get_bot() -> Bot:
    """Get or create Telegram Bot instance."""
    global _bot
    if _bot is None:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot


def _build_caption(screen_name: str, tweet) -> str:
    """Build formatted Telegram caption for a tweet."""
    tweet_url = f"https://x.com/{screen_name}/status/{tweet.id}"
    text = str(tweet.text) if tweet.text else ""

    # Escape HTML special chars to avoid parse errors
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if len(text) > 800:
        text = text[:800] + "..."

    caption = f"🐦 <b>@{screen_name}</b>\n\n{text}\n\n🔗 <a href=\"{tweet_url}\">Xem trên X</a>"

    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    return caption


async def send_tweet(tweet, screen_name: str, media_info: dict) -> bool:
    """Send a tweet to Telegram with appropriate media.

    Returns True if sent successfully.
    """
    bot = _get_bot()
    caption = _build_caption(screen_name, tweet)
    media_type = media_info.get("type", "none")

    try:
        if media_type == "photo":
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=media_info["urls"][0],
                caption=caption,
                parse_mode=ParseMode.HTML,
                read_timeout=60,
                write_timeout=60,
            )

        elif media_type == "photos":
            media_group = []
            for i, url in enumerate(media_info["urls"][:10]):
                if i == 0:
                    media_group.append(
                        InputMediaPhoto(
                            media=url,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                        )
                    )
                else:
                    media_group.append(InputMediaPhoto(media=url))

            await bot.send_media_group(
                chat_id=TELEGRAM_CHAT_ID,
                media=media_group,
            )

        elif media_type in ("video", "gif"):
            file_path = media_info.get("file_path")
            if file_path and os.path.exists(file_path):
                with open(file_path, "rb") as video_file:
                    await bot.send_video(
                        chat_id=TELEGRAM_CHAT_ID,
                        video=video_file,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        read_timeout=120,
                        write_timeout=120,
                    )
                _cleanup_file(file_path)
            else:
                logger.warning("Video file not found: %s, sending text only", file_path)
                await _send_text_only(bot, caption)

        else:
            await _send_text_only(bot, caption)

        logger.info("Sent tweet %s from @%s to Telegram", tweet.id, screen_name)
        return True

    except Exception as e:
        logger.error(
            "Failed to send tweet %s from @%s: %s", tweet.id, screen_name, e
        )
        _cleanup_file(media_info.get("file_path"))
        return False


async def _send_text_only(bot: Bot, caption: str) -> None:
    """Send a text-only message to Telegram."""
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=caption,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )


def _cleanup_file(file_path: str | None) -> None:
    """Remove a temp file if it exists."""
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass
