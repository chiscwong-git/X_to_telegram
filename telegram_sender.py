import asyncio
import logging
import os

from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode

from config import MEDIA_TEMP_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS

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


async def _send_to_chat(bot: Bot, chat_id: str, media_type: str, caption: str, media_info: dict) -> bool:
    """Send a tweet message to a single chat_id."""
    try:
        if media_type == "photo":
            await bot.send_photo(
                chat_id=chat_id,
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
                        InputMediaPhoto(media=url, caption=caption, parse_mode=ParseMode.HTML)
                    )
                else:
                    media_group.append(InputMediaPhoto(media=url))
            await bot.send_media_group(chat_id=chat_id, media=media_group)

        elif media_type in ("video", "gif"):
            file_path = media_info.get("file_path")
            if file_path and os.path.exists(file_path):
                with open(file_path, "rb") as video_file:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=video_file,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        read_timeout=120,
                        write_timeout=120,
                    )
            else:
                await bot.send_message(
                    chat_id=chat_id, text=caption,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=False,
                )

        else:
            await bot.send_message(
                chat_id=chat_id, text=caption,
                parse_mode=ParseMode.HTML, disable_web_page_preview=False,
            )
        return True

    except Exception as e:
        logger.error("Failed to send tweet to chat %s: %s", chat_id, e)
        return False


async def send_tweet(tweet, screen_name: str, media_info: dict) -> bool:
    """Send a tweet to ALL configured Telegram chats.

    Returns True if at least one delivery succeeded.
    """
    bot = _get_bot()
    caption = _build_caption(screen_name, tweet)
    media_type = media_info.get("type", "none")
    file_path = media_info.get("file_path")

    results = await asyncio.gather(
        *[_send_to_chat(bot, cid, media_type, caption, media_info) for cid in TELEGRAM_CHAT_IDS]
    )

    _cleanup_file(file_path)
    success = any(results)
    if success:
        logger.info("Sent tweet %s from @%s to %d chat(s)", tweet.id, screen_name, sum(results))
    return success




async def _send_text_only(bot: Bot, chat_id: str, caption: str) -> None:
    """Send a text-only message to a single chat."""
    await bot.send_message(
        chat_id=chat_id,
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


def _build_fb_caption(post: dict) -> str:
    """Build Telegram caption for a Facebook post."""
    page_name = post.get("page_slug", "").replace("fb_", "")
    text = (post.get("text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    post_url = post.get("post_url", "")

    if len(text) > 800:
        text = text[:800] + "..."

    caption = f"📘 <b>{page_name}</b>\n\n{text}"
    if post_url:
        caption += f'\n\n🔗 <a href="{post_url}">Xem trên Facebook</a>'

    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    return caption


async def _send_fb_to_chat(bot: Bot, chat_id: str, post: dict, caption: str, video_file_path: str | None) -> bool:
    """Send a Facebook post to a single chat_id."""
    try:
        if video_file_path and os.path.exists(video_file_path):
            with open(video_file_path, "rb") as vf:
                await bot.send_video(
                    chat_id=chat_id, video=vf, caption=caption,
                    parse_mode=ParseMode.HTML, read_timeout=120, write_timeout=120,
                )

        elif len(post.get("image_urls", [])) > 1:
            media_group = []
            for i, url in enumerate(post["image_urls"][:10]):
                if i == 0:
                    media_group.append(
                        InputMediaPhoto(media=url, caption=caption, parse_mode=ParseMode.HTML)
                    )
                else:
                    media_group.append(InputMediaPhoto(media=url))
            await bot.send_media_group(chat_id=chat_id, media=media_group)

        elif post.get("image_urls"):
            await bot.send_photo(
                chat_id=chat_id, photo=post["image_urls"][0], caption=caption,
                parse_mode=ParseMode.HTML, read_timeout=60, write_timeout=60,
            )

        else:
            await bot.send_message(
                chat_id=chat_id, text=caption,
                parse_mode=ParseMode.HTML, disable_web_page_preview=False,
            )
        return True

    except Exception as e:
        logger.error("Failed to send FB post to chat %s: %s", chat_id, e)
        return False


async def send_fb_post(post: dict, video_file_path: str | None = None) -> bool:
    """Send a Facebook post to ALL configured Telegram chats.

    Returns True if at least one delivery succeeded.
    """
    bot = _get_bot()
    caption = _build_fb_caption(post)
    post_id = post.get("id", "?")
    slug = post.get("page_slug", "fb_page")

    results = await asyncio.gather(
        *[_send_fb_to_chat(bot, cid, post, caption, video_file_path) for cid in TELEGRAM_CHAT_IDS]
    )

    _cleanup_file(video_file_path)
    success = any(results)
    if success:
        logger.info("Sent FB post %s from %s to %d chat(s)", post_id, slug, sum(results))
    else:
        logger.error("Failed to send FB post %s from %s to any chat", post_id, slug)
    return success

