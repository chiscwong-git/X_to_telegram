"""
Facebook Fanpage Scraper using Playwright.
Uses storage_state for robust session persistence (no re-login after first run).
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from config import FB_COOKIES_FILE, FB_PASSWORD, FB_USERNAME, MEDIA_TEMP_DIR

logger = logging.getLogger(__name__)

# fb_cookies.json stores the full Playwright storage_state
STATE_FILE = FB_COOKIES_FILE

_playwright = None
_browser: Browser | None = None
_context: BrowserContext | None = None


def _page_slug(page_url: str) -> str:
    """https://www.facebook.com/tintucUSstock → fb_tintucusstock"""
    path = urlparse(page_url).path.strip("/").split("/")[0]
    return f"fb_{path.lower()}"


async def _save_state() -> None:
    """Persist full browser state (cookies + localStorage + sessionStorage)."""
    if _context is None:
        return
    await _context.storage_state(path=STATE_FILE)
    logger.info("Saved browser state to %s", STATE_FILE)


async def _get_context() -> BrowserContext:
    """Get or lazily create a persistent Playwright browser context."""
    global _playwright, _browser, _context

    if _context is not None:
        return _context

    _playwright = await async_playwright().start()

    has_state = os.path.exists(STATE_FILE)

    _browser = await _playwright.chromium.launch(
        headless=True,  # always headless — setup_fb.py handles first-time login
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    context_opts = dict(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="vi-VN",
    )

    if has_state:
        # Restore full session — no login required
        _context = await _browser.new_context(storage_state=STATE_FILE, **context_opts)
        logger.info("Restored session from %s (headless)", STATE_FILE)
    else:
        _context = await _browser.new_context(**context_opts)
        logger.info("No saved session found — will open browser for login")

    return _context


async def _is_logged_in(page: Page) -> bool:
    """Check for Facebook session cookies (c_user = numeric user ID)."""
    try:
        cookies = await page.context.cookies()
        c_user = next((c for c in cookies if c["name"] == "c_user"), None)
        if c_user and c_user["value"].isdigit():
            return True
        # Fallback: not on login page
        if "login" not in page.url and "checkpoint" not in page.url:
            if await page.query_selector('[aria-label="Facebook"]') is not None:
                return True
        return False
    except Exception:
        return False


async def login_facebook() -> bool:
    """Login to Facebook and save session state for future runs."""
    if not FB_USERNAME or not FB_PASSWORD:
        logger.error("FB_USERNAME and FB_PASSWORD must be set in .env")
        return False

    context = await _get_context()
    page = await context.new_page()

    try:
        logger.info("Opening Facebook for login...")
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        if await _is_logged_in(page):
            logger.info("Already logged in via session state")
            await _save_state()
            await page.close()
            return True

        # Accept cookie consent if shown
        for sel in ['button:has-text("Allow all cookies")', 'button:has-text("Chấp nhận tất cả")']:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1)
                    break
            except Exception:
                pass

        # Fill credentials (try multiple selectors)
        logger.info("Filling credentials...")
        for sel in ['input[name="email"]', '#email', 'input[type="email"]']:
            try:
                await page.wait_for_selector(sel, timeout=8000)
                await page.fill(sel, FB_USERNAME)
                logger.info("Email filled via: %s", sel)
                break
            except Exception:
                continue
        else:
            logger.error("Could not find email input")
            await page.close()
            return False

        await asyncio.sleep(0.5)

        for sel in ['input[name="pass"]', '#pass', 'input[type="password"]']:
            try:
                await page.fill(sel, FB_PASSWORD)
                break
            except Exception:
                continue

        await asyncio.sleep(0.5)

        for sel in ['[name="login"]', 'button[type="submit"]', 'input[type="submit"]']:
            try:
                await page.click(sel, timeout=5000)
                break
            except Exception:
                continue

        # Wait for navigation
        await page.wait_for_load_state("domcontentloaded", timeout=25000)
        await asyncio.sleep(4)

        if await _is_logged_in(page):
            logger.info("✓ Facebook login successful!")
            await _save_state()  # Save full state — won't need to login again
            await page.close()
            return True
        else:
            logger.error("Login failed — check credentials or if Facebook requires 2FA/captcha")
            await page.close()
            return False

    except Exception as e:
        logger.error("Login error: %s", e)
        try:
            await page.close()
        except Exception:
            pass
        return False


async def init_fb_client() -> bool:
    """Initialize Facebook client. Returns True if logged in and ready."""
    context = await _get_context()
    page = await context.new_page()

    try:
        logger.info("Checking Facebook session...")
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        if await _is_logged_in(page):
            logger.info("✓ Facebook session valid (no login needed)")
            await page.close()
            return True

        await page.close()
        logger.info("Session expired or missing — logging in...")
        return await login_facebook()

    except Exception as e:
        logger.error("FB init error: %s", e)
        try:
            await page.close()
        except Exception:
            pass
        return False


# ─── Post extraction ──────────────────────────────────────────────────────────

def _extract_post_id(url: str) -> str | None:
    """Extract a stable post ID from a Facebook post URL."""
    for pattern in [
        r"/posts/([^/?&#]+)",
        r"pfbid([A-Za-z0-9]+)",
        r"story_fbid=(\d+)",
        r"fbid=(\d+)",
        r"/(\d{10,})",
    ]:
        m = re.search(pattern, url)
        if m:
            val = m.group(1)
            return ("pfbid" + val) if "pfbid" in pattern else val
    return None


async def get_new_posts(page_url: str, last_post_id: str | None) -> list[dict]:
    """Scrape recent posts from a fanpage. Returns new posts oldest-first."""
    context = await _get_context()
    page = await context.new_page()
    posts_url = page_url.rstrip("/") + "/posts"
    slug = _page_slug(page_url)

    try:
        logger.info("Fetching %s...", posts_url)
        await page.goto(posts_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)
        await _dismiss_popups(page)

        # Wait for posts to render
        try:
            await page.wait_for_selector('div[role="article"]', timeout=15000)
        except Exception:
            logger.warning("No articles found on %s", posts_url)
            await page.close()
            return []

        articles = await page.query_selector_all('div[role="article"]')
        logger.info("Found %d article element(s)", len(articles))

        # Only keep TOP-LEVEL articles (not nested inside another article = not comments)
        top_level = []
        for article in articles:
            is_nested = await article.evaluate(
                """el => {
                    let p = el.parentElement;
                    while (p) {
                        if (p !== el && p.getAttribute && p.getAttribute('role') === 'article') return true;
                        p = p.parentElement;
                    }
                    return false;
                }"""
            )
            if not is_nested:
                top_level.append(article)

        logger.info("Found %d top-level post(s)", len(top_level))

        collected = []
        seen_ids: set[str] = set()

        for article in top_level[:8]:
            try:
                post = await _extract_post_from_article(page, article, slug)
                if not post or not post.get("id"):
                    continue
                if post["id"] in seen_ids:
                    continue
                seen_ids.add(post["id"])
                collected.append(post)
            except Exception as e:
                logger.debug("Article extraction error: %s", e)

        await page.close()

        # First run: return all (caller decides)
        if last_post_id is None:
            return collected

        # Set-based filter: posts whose ID is NOT in the already-seen set
        # collected is newest-first; stop once we hit last_post_id
        seen_set = {last_post_id}
        new_posts = []
        for post in collected:
            if post["id"] in seen_set:
                break  # reached last known post, older posts irrelevant
            new_posts.append(post)

        new_posts.reverse()  # send oldest first

        if new_posts:
            logger.info("%d new FB post(s) from %s", len(new_posts), slug)
        else:
            logger.debug("No new FB posts from %s", slug)
        return new_posts

    except Exception as e:
        logger.error("Scrape error for %s: %s", posts_url, e)
        try:
            await page.close()
        except Exception:
            pass
        return []


async def _dismiss_popups(page: Page) -> None:
    """Close login/notification popups and remove scroll locks."""
    for sel in [
        '[aria-label="Close"]', '[aria-label="Đóng"]',
        'div[role="dialog"] [role="button"]:has-text("Not Now")',
        'div[role="dialog"] [role="button"]:has-text("Để sau")',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1200):
                await btn.click()
                await asyncio.sleep(0.4)
        except Exception:
            pass
    try:
        await page.evaluate("document.body.style.overflow='auto';document.documentElement.style.overflow='auto'")
    except Exception:
        pass


async def _click_see_more(page: Page, article) -> None:
    """Click 'Xem thêm' / 'See more' inside an article to expand truncated text."""
    see_more_texts = [
        "Xem thêm", "See more", "See More",
        "Xem thêm...", "...See more", "Xem tiếp",
    ]
    try:
        for text in see_more_texts:
            # Use page.locator to scope to article
            btn = page.locator(f'div[role="article"] >> text="{text}"').first
            if await btn.is_visible(timeout=800):
                await btn.click(timeout=3000)
                await asyncio.sleep(0.8)
                logger.debug("Clicked '%s' to expand post", text)
                return
    except Exception:
        pass

    # Fallback: look for div[role="button"] containing the text inside article
    try:
        buttons = await article.query_selector_all('div[role="button"], span[role="button"]')
        for btn_el in buttons:
            btn_text = (await btn_el.inner_text()).strip()
            if btn_text.lower() in ["xem thêm", "see more", "xem tiếp"]:
                await btn_el.click(timeout=2000)
                await asyncio.sleep(0.8)
                logger.debug("Clicked See More via element text")
                return
    except Exception:
        pass


async def _extract_post_from_article(page: Page, article, slug: str) -> dict | None:
    """Extract post data from an article element."""
    post_url = post_id = ""

    links = await article.query_selector_all("a[href]")
    for link in links:
        href = await link.get_attribute("href") or ""
        if any(x in href for x in ["/posts/", "/photo", "/video", "/reel", "story_fbid", "pfbid"]):
            if "comment_id" not in href and "/login" not in href:
                post_url = href.split("?")[0]
                post_id = _extract_post_id(href) or ""
                if post_id:
                    break

    # Fallback: any link with numeric ID
    if not post_id:
        for link in links:
            href = await link.get_attribute("href") or ""
            pid = _extract_post_id(href)
            if pid:
                post_url = href.split("?")[0]
                post_id = pid
                break

    if not post_id:
        return None

    if post_url and not post_url.startswith("http"):
        post_url = "https://www.facebook.com" + post_url

    # Expand truncated content before extracting text
    await _click_see_more(page, article)

    # ── Text extraction ───────────────────────────────────────────────────────
    # Strategy: use the comment-input box as a DOM boundary.
    # Everything BEFORE it belongs to the post body; everything AFTER is comments.
    text = await article.evaluate(
        """(article) => {
            // ① Official comet message attribute (most precise when present)
            const msgs = Array.from(article.querySelectorAll(
                '[data-ad-comet-preview="message"], [data-ad-preview="message"]'
            ));
            if (msgs.length > 0) {
                return msgs.map(m => m.innerText.trim()).filter(Boolean)
                       .join('\\n\\n');
            }

            // ② Use the comment input box as DOM boundary
            const commentBox = article.querySelector(
                '[aria-label*="comment"], [aria-label*="bình luận"], ' +
                '[aria-label*="Bình luận"], [aria-label*="Write a comment"], ' +
                '[aria-label*="Viết bình luận"], [aria-placeholder*="comment"], ' +
                '[contenteditable="true"]'
            );

            const leafBlocks = Array.from(
                article.querySelectorAll('div[dir="auto"], span[dir="auto"]')
            );

            const content = [];
            for (const block of leafBlocks) {
                // Only leaf-level text nodes
                if (block.querySelector('div[dir="auto"], span[dir="auto"]')) continue;

                // If a comment box exists, skip anything that comes AFTER it in DOM order
                if (commentBox) {
                    const pos = commentBox.compareDocumentPosition(block);
                    // DOCUMENT_POSITION_FOLLOWING = 4
                    if (pos & 4) continue; // block is AFTER commentBox → skip
                }

                const t = block.innerText.trim();
                if (t.length < 8) continue;

                // Filter out UI chrome
                const uiWords = ['Like', 'Reply', 'Share', 'Follow', 'Comment',
                                 'Send', 'See more', 'Xem thêm', 'Xem tiếp'];
                if (uiWords.includes(t)) continue;

                // Filter timestamps like "2h ·", "Just now"
                if (/^\d+[mhds] ?[·•]?$/.test(t)) continue;

                if (!content.includes(t)) content.push(t);
            }

            return content.join('\\n\\n');
        }"""
    )

    # ── Image extraction ──────────────────────────────────────────────────────
    # Use JS so we can check naturalWidth (rendered size) to exclude avatars.
    # Profile pictures / comment avatars are tiny (< 100px).
    # Post images are large (> 100px).
    image_urls = await article.evaluate(
        """(article) => {
            const commentBox = article.querySelector(
                '[aria-label*="comment"], [aria-label*="bình luận"], ' +
                '[aria-label*="Bình luận"], [aria-label*="Write a comment"], ' +
                '[contenteditable="true"]'
            );

            return Array.from(article.querySelectorAll('img[src]'))
                .filter(img => {
                    const src = img.src || '';
                    if (!src.includes('scontent') && !src.includes('fbcdn')) return false;

                    // Exclude small avatars by rendered size
                    if (img.naturalWidth > 0 && img.naturalWidth < 100) return false;
                    if (img.naturalHeight > 0 && img.naturalHeight < 100) return false;

                    // Exclude explicit thumbnail URL patterns (profile pics)
                    if (/\/p(40|50|60|80|100|120|160)x(40|50|60|80|100|120|160)\//.test(src)) return false;
                    if (/\/s(40|50|60|80|100|120|160)x(40|50|60|80|100|120|160)\//.test(src)) return false;

                    // Exclude if image comes AFTER the comment input box
                    if (commentBox) {
                        const pos = commentBox.compareDocumentPosition(img);
                        if (pos & 4) return false; // img is AFTER commentBox
                    }

                    return true;
                })
                .map(img => img.src.replace(/_s\./, '_n.'))  // prefer higher res
                .filter((v, i, a) => a.indexOf(v) === i);    // unique
        }"""
    )

    has_video = await article.query_selector("video, [data-video-id]") is not None

    return {
        "id": post_id,
        "post_url": post_url or f"https://www.facebook.com/{slug.replace('fb_','')}/posts/{post_id}",
        "text": text,
        "image_urls": image_urls[:10],
        "has_video": has_video,
        "page_slug": slug,
    }


async def download_fb_video(post_url: str) -> str | None:
    """Download a Facebook video using yt-dlp."""
    os.makedirs(MEDIA_TEMP_DIR, exist_ok=True)
    url_hash = abs(hash(post_url)) % 10 ** 9
    file_path = os.path.join(MEDIA_TEMP_DIR, f"fb_video_{url_hash}.mp4")

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["yt-dlp", "--no-warnings", "-f", "best[filesize<50M]/best",
             "-o", file_path, "--no-playlist", post_url],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and os.path.exists(file_path):
            logger.info("Downloaded FB video: %s", file_path)
            return file_path
        logger.warning("yt-dlp failed: %s", result.stderr[:200])
    except Exception as e:
        logger.error("yt-dlp error: %s", e)
    return None


async def close_fb_client() -> None:
    """Gracefully close browser and playwright."""
    global _browser, _context, _playwright
    for obj, name in [(_context, "context"), (_browser, "browser"), (_playwright, "playwright")]:
        if obj:
            try:
                await obj.close()
            except Exception:
                pass
    _context = _browser = _playwright = None
