#!/usr/bin/env python3
"""Telegram Bot service that polls Helakuru Esana news and posts updates to a Telegram chat/channel."""

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from esana_rss import fetch_esana_news, format_transcript_keys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("TgRss")

TG_MAX_CAPTION_LEN = 1024
TG_MAX_MESSAGE_LEN = 4096

CATEGORY_HASHTAGS_SI = {
    2: "#සිද්ධි",
    3: "#හඬපට",
    4: "#නිවේදන",
    5: "#ප්‍රකාශ",
}

CATEGORY_HASHTAGS_EN = {
    2: "#Incidents",
    3: "#Voice",
    4: "#Announcements",
    5: "#Statements",
}


def load_dotenv(dotenv_path: Path) -> None:
    """Load key=value pairs from a .env file into os.environ if not already set."""
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


class StateDB:
    """SQLite store tracking posted Esana news IDs, Telegram message IDs, and content hashes for auto-edit."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posted_news (
                news_id INTEGER PRIMARY KEY,
                title TEXT,
                published TEXT,
                message_id INTEGER,
                continuation_msg_id INTEGER,
                content_hash TEXT,
                extra_images_json TEXT,
                disc_chat_id INTEGER,
                comment_msg_id INTEGER,
                comment_hash TEXT,
                reaction_msg_id INTEGER,
                reaction_hash TEXT,
                comment_synced_at REAL,
                voice_msg_id INTEGER,
                voice_url TEXT,
                timestamp_msg_id INTEGER,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Ensure columns exist when upgrading from an older esana_state.db schema."""
        cur = self.conn.execute("PRAGMA table_info(posted_news)")
        existing_cols = {row[1] for row in cur.fetchall()}
        migrations = {
            "message_id": "INTEGER",
            "continuation_msg_id": "INTEGER",
            "content_hash": "TEXT",
            "extra_images_json": "TEXT",
            "disc_chat_id": "INTEGER",
            "comment_msg_id": "INTEGER",
            "comment_hash": "TEXT",
            "reaction_msg_id": "INTEGER",
            "reaction_hash": "TEXT",
            "comment_synced_at": "REAL",
            "voice_msg_id": "INTEGER",
            "voice_url": "TEXT",
            "timestamp_msg_id": "INTEGER",
        }
        for col, col_type in migrations.items():
            if col not in existing_cols:
                self.conn.execute(f"ALTER TABLE posted_news ADD COLUMN {col} {col_type}")

    def is_empty(self) -> bool:
        cur = self.conn.execute("SELECT COUNT(*) FROM posted_news")
        return cur.fetchone()[0] == 0

    def get_record(self, news_id: int) -> dict | None:
        cur = self.conn.execute(
            """
            SELECT news_id, title, published, message_id, continuation_msg_id, content_hash,
                   extra_images_json, disc_chat_id, comment_msg_id, comment_hash,
                   reaction_msg_id, reaction_hash, comment_synced_at, voice_msg_id, voice_url,
                   timestamp_msg_id
            FROM posted_news WHERE news_id = ?
            """,
            (news_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        extra_imgs = []
        if row[6]:
            try:
                extra_imgs = json.loads(row[6])
            except Exception:
                extra_imgs = []
        return {
            "news_id": row[0],
            "title": row[1],
            "published": row[2],
            "message_id": row[3],
            "continuation_msg_id": row[4],
            "content_hash": row[5],
            "extra_images": extra_imgs,
            "disc_chat_id": row[7],
            "comment_msg_id": row[8],
            "comment_hash": row[9],
            "reaction_msg_id": row[10],
            "reaction_hash": row[11],
            "comment_synced_at": row[12] or 0.0,
            "voice_msg_id": row[13],
            "voice_url": row[14],
            "timestamp_msg_id": row[15] if len(row) > 15 else None,
        }

    def is_posted(self, news_id: int) -> bool:
        cur = self.conn.execute("SELECT 1 FROM posted_news WHERE news_id = ?", (news_id,))
        return cur.fetchone() is not None

    def save_posted(
        self,
        news_id: int,
        title: str,
        published: str,
        message_id: int | None = None,
        continuation_msg_id: int | None = None,
        content_hash: str | None = None,
        extra_images: list[str] | None = None,
        disc_chat_id: int | None = None,
        comment_msg_id: int | None = None,
        comment_hash: str | None = None,
        reaction_msg_id: int | None = None,
        reaction_hash: str | None = None,
        comment_synced_at: float | None = None,
        voice_msg_id: int | None = None,
        voice_url: str | None = None,
        timestamp_msg_id: int | None = None,
    ) -> None:
        extra_json = json.dumps(extra_images or [])
        self.conn.execute(
            """
            INSERT INTO posted_news (
                news_id, title, published, message_id, continuation_msg_id, content_hash,
                extra_images_json, disc_chat_id, comment_msg_id, comment_hash,
                reaction_msg_id, reaction_hash, comment_synced_at, voice_msg_id, voice_url,
                timestamp_msg_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(news_id) DO UPDATE SET
                title = excluded.title,
                published = excluded.published,
                message_id = COALESCE(excluded.message_id, posted_news.message_id),
                continuation_msg_id = COALESCE(excluded.continuation_msg_id, posted_news.continuation_msg_id),
                content_hash = COALESCE(excluded.content_hash, posted_news.content_hash),
                extra_images_json = excluded.extra_images_json,
                disc_chat_id = COALESCE(excluded.disc_chat_id, posted_news.disc_chat_id),
                comment_msg_id = COALESCE(excluded.comment_msg_id, posted_news.comment_msg_id),
                comment_hash = COALESCE(excluded.comment_hash, posted_news.comment_hash),
                reaction_msg_id = COALESCE(excluded.reaction_msg_id, posted_news.reaction_msg_id),
                reaction_hash = COALESCE(excluded.reaction_hash, posted_news.reaction_hash),
                comment_synced_at = COALESCE(excluded.comment_synced_at, posted_news.comment_synced_at),
                voice_msg_id = COALESCE(excluded.voice_msg_id, posted_news.voice_msg_id),
                voice_url = COALESCE(excluded.voice_url, posted_news.voice_url),
                timestamp_msg_id = COALESCE(excluded.timestamp_msg_id, posted_news.timestamp_msg_id)
            """,
            (
                news_id,
                title,
                published,
                message_id,
                continuation_msg_id,
                content_hash,
                extra_json,
                disc_chat_id,
                comment_msg_id,
                comment_hash,
                reaction_msg_id,
                reaction_hash,
                comment_synced_at,
                voice_msg_id,
                voice_url,
                timestamp_msg_id,
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class TelegramHTMLSanitizer(HTMLParser):
    """
    Sanitize and convert HTML from news content into valid Telegram HTML entities.
    Preserves supported formatting tags (a, b, strong, i, em, u, ins, s, strike, del, code, pre, blockquote),
    strips unsupported tags, converts href attributes to double-quoted attributes,
    and escapes raw &, <, > characters in text without escaping quotes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.result: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "a":
            attr_dict = dict(attrs)
            href = (attr_dict.get("href") or "").strip()
            if href:
                self.result.append(f'<a href="{html.escape(href, quote=True)}">')
        elif tag in ("b", "strong"):
            self.result.append("<b>")
        elif tag in ("i", "em"):
            self.result.append("<i>")
        elif tag in ("u", "ins"):
            self.result.append("<u>")
        elif tag in ("s", "strike", "del"):
            self.result.append("<s>")
        elif tag in ("code", "pre", "blockquote"):
            self.result.append(f"<{tag}>")
        elif tag == "br":
            self.result.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a":
            self.result.append("</a>")
        elif tag in ("b", "strong"):
            self.result.append("</b>")
        elif tag in ("i", "em"):
            self.result.append("</i>")
        elif tag in ("u", "ins"):
            self.result.append("</u>")
        elif tag in ("s", "strike", "del"):
            self.result.append("</s>")
        elif tag in ("code", "pre", "blockquote"):
            self.result.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.result.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        char = html.unescape(f"&{name};")
        self.result.append(html.escape(char, quote=False))

    def handle_charref(self, name: str) -> None:
        char = html.unescape(f"&#{name};")
        self.result.append(html.escape(char, quote=False))

    def get_html(self) -> str:
        return "".join(self.result)


def sanitize_telegram_html(raw_text: str) -> str:
    """Safely sanitize text into Telegram HTML, preserving valid tags like <a href="..."> and bolding."""
    if not raw_text:
        return ""
    clean = raw_text.replace("\xa0", " ")
    sanitizer = TelegramHTMLSanitizer()
    try:
        sanitizer.feed(clean)
        sanitizer.close()
        return sanitizer.get_html()
    except Exception:
        return html.escape(clean, quote=False)


def extract_news_parts(
    item: dict, lang: str = "si"
) -> tuple[str, list[str], str, list[str], str | None, list[dict], str, str, str]:
    """
    Extract structured fields from an Esana news item.

    Returns:
        (title, paragraphs, cover_url, extra_images, voice_url, voice_keys, published, share_url, hashtag)
    """
    news_id = item.get("id")
    if lang == "en":
        title = (item.get("titleEn") or item.get("titleSi") or f"News #{news_id}").strip()
        blocks = item.get("contentEn") or item.get("contentSi") or []
    else:
        title = (item.get("titleSi") or item.get("titleEn") or f"News #{news_id}").strip()
        blocks = item.get("contentSi") or item.get("contentEn") or []

    cover_url = (item.get("cover") or item.get("thumb") or "").strip()
    share_url = (item.get("share_url") or f"https://www.helakuru.lk/esana/news/{news_id}").strip()
    published = (item.get("published") or "").strip()

    cat_id = int(item.get("category") or 0)
    hashtag_map = CATEGORY_HASHTAGS_EN if lang == "en" else CATEGORY_HASHTAGS_SI
    hashtag = hashtag_map.get(cat_id, "")

    paragraphs: list[str] = []
    extra_images: list[str] = []
    voice_url: str | None = None
    voice_keys: list[dict] = []

    for block in blocks:
        btype = block.get("type")
        bdata = (block.get("data") or "").strip()
        if btype == "text":
            if bdata:
                paragraphs.append(sanitize_telegram_html(bdata))
        elif btype == "image":
            if bdata:
                if not cover_url:
                    cover_url = bdata
                elif bdata != cover_url and bdata not in extra_images:
                    extra_images.append(bdata)
        elif btype in ("voice", "audio"):
            if bdata and bdata.startswith("http") and not voice_url:
                voice_url = bdata
            keys = block.get("keys") or []
            if keys:
                if not voice_keys:
                    voice_keys = keys
                if lang == "en":
                    transcript_paras = format_transcript_keys(keys)
                    for p in transcript_paras:
                        paragraphs.append(sanitize_telegram_html(p))
            elif bdata and not bdata.startswith("http") and lang == "en":
                paragraphs.append(sanitize_telegram_html(bdata))

    # Fallback to check other language content blocks for voice_url / keys if not found
    if not voice_url or not voice_keys:
        other_blocks = item.get("contentSi") if lang == "en" else item.get("contentEn")
        for block in other_blocks or []:
            if block.get("type") in ("voice", "audio"):
                bdata = (block.get("data") or "").strip()
                if bdata and bdata.startswith("http") and not voice_url:
                    voice_url = bdata
                if block.get("keys") and not voice_keys:
                    voice_keys = block.get("keys")

    return title, paragraphs, cover_url, extra_images, voice_url, voice_keys, published, share_url, hashtag


def build_footer(hashtag: str, channel_tag: str) -> str:
    """Build the post footer with category hashtag and channel username."""
    lines: list[str] = []
    if hashtag:
        lines.append(html.escape(hashtag, quote=False))
    if channel_tag:
        lines.append(html.escape(channel_tag, quote=False))
    return "\n\n".join(lines)


def split_caption_and_continuation(
    header: str,
    paragraphs: list[str],
    footer: str,
    channel_tag: str = "",
    max_caption_len: int = TG_MAX_CAPTION_LEN,
) -> tuple[str, str | None]:
    """
    If the full post fits in <= max_caption_len chars, return (full_caption, None).
    Otherwise:
      - Photo caption gets header + as many paragraphs as fit + channel_tag (@HelakuruEsanaNews).
      - Continuation reply (last message) gets remaining paragraphs + footer (#Category\n\n@HelakuruEsanaNews).
    """
    body = "\n\n".join(paragraphs)
    full = f"{header}\n\n{body}\n\n{footer}" if body else f"{header}\n\n{footer}"
    if len(full) <= max_caption_len:
        return full, None

    photo_footer = channel_tag
    last_suffix = footer

    kept: list[str] = []
    for p in paragraphs:
        candidate_body = "\n\n".join(kept + [p])
        candidate = f"{header}\n\n{candidate_body}\n\n{photo_footer}" if photo_footer else f"{header}\n\n{candidate_body}"
        if len(candidate) > max_caption_len:
            break
        kept.append(p)

    if not kept and paragraphs:
        # First paragraph alone is huge; split it at a word boundary
        avail_len = max_caption_len - len(header) - (len(f"\n\n{photo_footer}") if photo_footer else 0) - 2
        avail = max(100, avail_len - 10)
        first_p = paragraphs[0]
        split_idx = first_p.rfind(" ", 0, avail)
        if split_idx <= 0:
            split_idx = avail
        part1 = first_p[:split_idx].rstrip() + "..."
        part2 = "..." + first_p[split_idx:].lstrip()
        caption = f"{header}\n\n{part1}\n\n{photo_footer}" if photo_footer else f"{header}\n\n{part1}"
        remaining_body = "\n\n".join([part2] + paragraphs[1:])
        continuation = f"{remaining_body}\n\n{last_suffix}" if last_suffix else remaining_body
        return caption, continuation[:TG_MAX_MESSAGE_LEN]

    caption_body = "\n\n".join(kept)
    if caption_body:
        caption = f"{header}\n\n{caption_body}\n\n{photo_footer}" if photo_footer else f"{header}\n\n{caption_body}"
    else:
        caption = f"{header}\n\n{photo_footer}" if photo_footer else header

    remaining_paragraphs = paragraphs[len(kept):]
    remaining_body = "\n\n".join(remaining_paragraphs)
    continuation = f"{remaining_body}\n\n{last_suffix}" if last_suffix else remaining_body
    return caption, continuation[:TG_MAX_MESSAGE_LEN]


def build_post_content(
    item: dict,
    lang: str = "si",
    channel_tag: str = "@HelakuruEsanaNews",
) -> tuple[str, str | None, str, list[str], str | None, list[dict], str, str]:
    """
    Build all post components for an Esana news item.

    Returns:
        (caption, continuation_text, cover_url, extra_images, voice_url, voice_keys, share_url, content_hash)
    """
    (
        title,
        paragraphs,
        cover_url,
        extra_images,
        voice_url,
        voice_keys,
        _published,
        share_url,
        hashtag,
    ) = extract_news_parts(item, lang=lang)
    header = f"<b>{html.escape(title, quote=False)}</b>"
    footer = build_footer(hashtag, channel_tag)
    channel_str = html.escape(channel_tag, quote=False) if channel_tag else ""
    caption, continuation = split_caption_and_continuation(
        header, paragraphs, footer=footer, channel_tag=channel_str, max_caption_len=TG_MAX_CAPTION_LEN
    )

    hash_input = json.dumps(
        {
            "caption": caption,
            "continuation": continuation or "",
            "cover": cover_url,
            "voice": voice_url or "",
            "voice_keys_count": len(voice_keys),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    content_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
    return caption, continuation, cover_url, extra_images, voice_url, voice_keys, share_url, content_hash


def sanitize_url(raw_url: str) -> str:
    """Safely encode URL path and query to prevent control character / invalid character errors."""
    if not raw_url:
        return ""
    parts = urllib.parse.urlsplit(raw_url.strip())
    safe_path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%:@&=+$,-_.!~*'()")
    safe_query = urllib.parse.quote(urllib.parse.unquote(parts.query), safe="/%:@&=+$,-_.!~*'()?#")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, safe_path, safe_query, parts.fragment))


def download_image_bytes(image_url: str) -> tuple[bytes, str]:
    """Download image bytes from Esana CDN (which serves a non-standard content-encoding header)."""
    safe_url = sanitize_url(image_url)
    req = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://www.helakuru.lk/",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = resp.read()
    raw_filename = urllib.parse.unquote(urllib.parse.urlsplit(safe_url).path).rsplit("/", 1)[-1] or "photo.jpg"
    filename = re.sub(r"[^\w\-.]", "_", raw_filename)
    if not filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        filename += ".jpg"
    return data, filename


def tg_api_request(bot_token: str, method: str, payload: dict) -> dict:
    """Call the Telegram Bot API with JSON payload and automatic retry on HTTP 429 rate limits."""
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")

    while True:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            if err.code == 429:
                try:
                    retry_after = json.loads(err_body).get("parameters", {}).get("retry_after", 5)
                except Exception:
                    retry_after = 5
                logger.warning("Telegram rate limit (429). Sleeping %s seconds...", retry_after)
                time.sleep(int(retry_after) + 1)
                continue
            raise RuntimeError(f"Telegram API {method} failed ({err.code}): {err_body}") from err


def tg_multipart_request(bot_token: str, method: str, fields: dict[str, str], files: list[tuple]) -> dict:
    """Send a multipart/form-data request to the Telegram Bot API with automatic 429 retry."""
    boundary = f"----TgRssBoundary{uuid.uuid4().hex}"
    body_parts: list[bytes] = []

    for k, v in fields.items():
        body_parts.append(f"--{boundary}\r\n".encode("utf-8"))
        body_parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
        body_parts.append(f"{v}\r\n".encode("utf-8"))

    for item in files:
        if len(item) == 4:
            field_name, filename, file_bytes, content_type = item
        else:
            field_name, filename, file_bytes = item
            ext = os.path.splitext(filename)[1].lower()
            if ext in (".jpg", ".jpeg"):
                content_type = "image/jpeg"
            elif ext == ".png":
                content_type = "image/png"
            elif ext == ".webp":
                content_type = "image/webp"
            elif ext == ".mp3":
                content_type = "audio/mpeg"
            elif ext == ".ogg":
                content_type = "audio/ogg"
            elif ext == ".m4a":
                content_type = "audio/mp4"
            else:
                content_type = "application/octet-stream"

        body_parts.append(f"--{boundary}\r\n".encode("utf-8"))
        body_parts.append(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
        )
        body_parts.append(file_bytes)
        body_parts.append(b"\r\n")

    body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(body_parts)

    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    while True:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            if err.code == 429:
                try:
                    retry_after = json.loads(err_body).get("parameters", {}).get("retry_after", 5)
                except Exception:
                    retry_after = 5
                logger.warning("Telegram rate limit (429). Sleeping %s seconds...", retry_after)
                time.sleep(int(retry_after) + 1)
                continue
            raise RuntimeError(f"Telegram API {method} failed ({err.code}): {err_body}") from err


def tg_send_photo(
    bot_token: str,
    chat_id: str,
    image_url: str,
    caption: str | None = None,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
) -> dict:
    """Download image from Esana CDN and upload bytes as multipart/form-data to Telegram sendPhoto."""
    img_bytes, filename = download_image_bytes(image_url)
    fields: dict[str, str] = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    if parse_mode:
        fields["parse_mode"] = parse_mode
    if reply_to_message_id:
        fields["reply_to_message_id"] = str(reply_to_message_id)

    return tg_multipart_request(bot_token, "sendPhoto", fields, [("photo", filename, img_bytes)])


def tg_send_media_group(
    bot_token: str,
    chat_id: str,
    image_urls: list[str],
    reply_to_message_id: int | None = None,
) -> None:
    """
    Send 2 or more extra images as a grouped Telegram photo album (sendMediaGroup),
    chunked in batches of up to 10 images.
    """
    for batch_start in range(0, len(image_urls), 10):
        batch = image_urls[batch_start : batch_start + 10]
        if len(batch) == 1:
            tg_send_photo(
                bot_token=bot_token,
                chat_id=chat_id,
                image_url=batch[0],
                reply_to_message_id=reply_to_message_id,
            )
            continue

        media_list = []
        files: list[tuple[str, str, bytes]] = []
        for idx, url in enumerate(batch):
            img_bytes, filename = download_image_bytes(url)
            attach_name = f"photo_{idx}"
            media_list.append({"type": "photo", "media": f"attach://{attach_name}"})
            files.append((attach_name, filename, img_bytes))

        fields: dict[str, str] = {
            "chat_id": str(chat_id),
            "media": json.dumps(media_list),
        }
        if reply_to_message_id:
            fields["reply_to_message_id"] = str(reply_to_message_id)

        tg_multipart_request(bot_token, "sendMediaGroup", fields, files)


def send_extra_images(
    bot_token: str,
    chat_id: str,
    extra_images: list[str],
    reply_to_message_id: int | None,
    news_id: int | str,
) -> None:
    """Send 1 extra image via sendPhoto or 2+ extra images as an album via sendMediaGroup."""
    if not extra_images:
        return
    time.sleep(1.0)
    try:
        if len(extra_images) == 1:
            tg_send_photo(
                bot_token=bot_token,
                chat_id=chat_id,
                image_url=extra_images[0],
                reply_to_message_id=reply_to_message_id,
            )
        else:
            tg_send_media_group(
                bot_token=bot_token,
                chat_id=chat_id,
                image_urls=extra_images,
                reply_to_message_id=reply_to_message_id,
            )
    except Exception as exc:
        logger.warning("Failed to send extra images for news #%s: %s", news_id, exc)


def download_audio_bytes(audio_url: str) -> tuple[bytes, str]:
    """Download audio bytes from Esana CDN."""
    safe_url = sanitize_url(audio_url)
    req = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://www.helakuru.lk/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    raw_filename = urllib.parse.unquote(urllib.parse.urlsplit(safe_url).path).rsplit("/", 1)[-1] or "voice.mp3"
    filename = re.sub(r"[^\w\-.]", "_", raw_filename)
    if not filename.lower().endswith((".mp3", ".ogg", ".m4a", ".wav", ".aac")):
        filename += ".mp3"
    return data, filename


def convert_to_ogg_opus(audio_bytes: bytes) -> bytes | None:
    """Convert audio bytes to OGG Opus using ffmpeg for native Telegram voice waveforms."""
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                "-b:a",
                "64k",
                "-f",
                "ogg",
                "pipe:1",
            ],
            input=audio_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except Exception as exc:
        logger.warning("ffmpeg conversion to ogg opus failed: %s", exc)
    return None


def tg_send_voice(
    bot_token: str,
    chat_id: str | int,
    voice_url: str,
    caption: str | None = None,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
    title: str | None = None,
    performer: str | None = "Helakuru Esana",
) -> dict:
    """
    Download voice file from Esana CDN and send it via Telegram sendVoice (or sendAudio fallback).
    Converts MP3 to OGG Opus via ffmpeg if available for native voice waveform support.
    """
    raw_bytes, raw_filename = download_audio_bytes(voice_url)
    ogg_bytes = convert_to_ogg_opus(raw_bytes)

    # 1. Try sendVoice with OGG Opus
    if ogg_bytes:
        try:
            fields: dict[str, str] = {"chat_id": str(chat_id)}
            if caption:
                fields["caption"] = caption
            if parse_mode:
                fields["parse_mode"] = parse_mode
            if reply_to_message_id:
                fields["reply_to_message_id"] = str(reply_to_message_id)

            return tg_multipart_request(
                bot_token=bot_token,
                method="sendVoice",
                fields=fields,
                files=[("voice", "voice.ogg", ogg_bytes, "audio/ogg")],
            )
        except Exception as exc:
            logger.warning("sendVoice (ogg) failed, trying raw audio: %s", exc)

    # 2. Try sendVoice with raw audio (mp3)
    try:
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
        if parse_mode:
            fields["parse_mode"] = parse_mode
        if reply_to_message_id:
            fields["reply_to_message_id"] = str(reply_to_message_id)

        content_type = "audio/mpeg" if raw_filename.endswith(".mp3") else "audio/ogg"
        return tg_multipart_request(
            bot_token=bot_token,
            method="sendVoice",
            fields=fields,
            files=[("voice", raw_filename, raw_bytes, content_type)],
        )
    except Exception as exc:
        logger.warning("sendVoice (raw) failed, falling back to sendAudio: %s", exc)

    # 3. Fallback to sendAudio
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    if parse_mode:
        fields["parse_mode"] = parse_mode
    if reply_to_message_id:
        fields["reply_to_message_id"] = str(reply_to_message_id)
    if title:
        fields["title"] = title[:64]
    if performer:
        fields["performer"] = performer

    content_type = "audio/mpeg" if raw_filename.endswith(".mp3") else "application/octet-stream"
    return tg_multipart_request(
        bot_token=bot_token,
        method="sendAudio",
        fields=fields,
        files=[("audio", raw_filename, raw_bytes, content_type)],
    )


def format_voice_timestamps_caption(
    keys: list[dict],
    channel_tag: str = "@HelakuruEsanaNews",
    max_len: int = 1024,
) -> str | None:
    """Format voice keys into timestamp chapter lines with channel username at bottom."""
    if not keys:
        return channel_tag

    lines: list[str] = []
    for k in keys:
        if not isinstance(k, dict):
            continue
        t = (k.get("time") or "").strip()
        txt = (k.get("text") or "").strip()
        if not txt:
            continue
        line = f"{t} - {txt}" if t else txt
        lines.append(line)
    if not lines:
        return channel_tag

    suffix = f"\n\n{channel_tag}" if channel_tag else ""
    avail_len = max_len - len(suffix)

    kept: list[str] = []
    for line in lines:
        candidate = "\n".join(kept + [line])
        if len(candidate) > avail_len:
            break
        kept.append(line)

    body = "\n".join(kept)
    if body and suffix:
        return f"{body}{suffix}"
    return body or channel_tag


def send_voice_audio(
    bot_token: str,
    chat_id: str | int,
    voice_url: str,
    reply_to_message_id: int | None = None,
    lang: str = "si",
    title: str | None = None,
    caption: str | None = None,
) -> int | None:
    """Download voice from Esana CDN and send to chat_id via sendVoice (or sendAudio). Returns message_id or None."""
    if not voice_url:
        return None
    time.sleep(1.0)
    try:
        res = tg_send_voice(
            bot_token=bot_token,
            chat_id=chat_id,
            voice_url=voice_url,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            title=title,
        )
        msg_id = (res.get("result") or {}).get("message_id")
        logger.info("Sent voice message (message_id=%s) for voice_url=%s", msg_id, voice_url)
        return msg_id
    except Exception as exc:
        logger.warning("Failed to send voice message for voice_url=%s: %s", voice_url, exc)
        return None


def send_timestamp_message(
    bot_token: str,
    chat_id: str | int,
    keys: list[dict],
    reply_to_message_id: int | None = None,
    lang: str = "si",
    channel_tag: str = "@HelakuruEsanaNews",
) -> int | None:
    """Send formatted timestamp chapter lines replying to the voice note."""
    if not keys:
        return None
    lines: list[str] = []
    for k in keys:
        if not isinstance(k, dict):
            continue
        t = (k.get("time") or "").strip()
        txt = (k.get("text") or "").strip()
        if not txt:
            continue
        if t:
            lines.append(f"{t} - {txt}")
        else:
            lines.append(txt)

    if not lines:
        return None

    full_text = "\n".join(lines)

    time.sleep(1.0)
    try:
        res = tg_api_request(
            bot_token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": full_text[:TG_MAX_MESSAGE_LEN],
                "reply_to_message_id": reply_to_message_id,
                "disable_web_page_preview": True,
            },
        )
        msg_id = (res.get("result") or {}).get("message_id")
        logger.info("Sent timestamp message (message_id=%s) for voice", msg_id)
        return msg_id
    except Exception as exc:
        logger.warning("Failed to send timestamp message: %s", exc)
        return None


def get_linked_discussion_chat_id(bot_token: str, chat_id: str) -> int | None:
    """Return the linked_chat_id (Discussion Group) of the channel if available."""
    try:
        res = tg_api_request(bot_token, "getChat", {"chat_id": chat_id})
        return (res.get("result") or {}).get("linked_chat_id")
    except Exception:
        return None


def find_discussion_thread_message(
    bot_token: str,
    chat_id: str,
    channel_message_id: int,
    max_wait_sec: float = 8.0,
) -> tuple[int | None, int | None]:
    """
    Find Telegram's auto-forwarded message for `channel_message_id` in the channel's
    linked Discussion Group via both getUpdates and getChat(linked_chat_id).pinned_message.

    Returns:
        (discussion_chat_id, discussion_message_id) or (None, None)
    """
    linked_chat_id = get_linked_discussion_chat_id(bot_token, chat_id)
    deadline = time.time() + max_wait_sec

    while time.time() < deadline:
        # 1) Check getUpdates using negative offset to always read the newest incoming updates
        try:
            res = tg_api_request(bot_token, "getUpdates", {"offset": -50, "limit": 50, "timeout": 1})
            for upd in reversed(res.get("result") or []):
                msg = upd.get("message") or upd.get("edited_message") or {}
                fwd_msg_id = msg.get("forward_from_message_id")
                if not fwd_msg_id and isinstance(msg.get("forward_origin"), dict):
                    fwd_msg_id = msg["forward_origin"].get("message_id")
                if (msg.get("is_automatic_forward") or fwd_msg_id) and fwd_msg_id == channel_message_id:
                    chat_id_val = (msg.get("chat") or {}).get("id") or linked_chat_id
                    return chat_id_val, msg.get("message_id")
        except Exception:
            pass

        # 2) Check pinned_message in the linked discussion group (works even if Bot Privacy Mode is enabled)
        if linked_chat_id:
            try:
                dchat = tg_api_request(bot_token, "getChat", {"chat_id": linked_chat_id})
                pinned = (dchat.get("result") or {}).get("pinned_message") or {}
                fwd_msg_id = pinned.get("forward_from_message_id")
                if not fwd_msg_id and isinstance(pinned.get("forward_origin"), dict):
                    fwd_msg_id = pinned["forward_origin"].get("message_id")
                if pinned.get("is_automatic_forward") and fwd_msg_id == channel_message_id:
                    return linked_chat_id, pinned.get("message_id")
            except Exception:
                pass

        time.sleep(0.8)
    return None, None


def build_english_comment(
    item: dict,
    channel_tag: str = "@HelakuruEsanaNews",
    share_url: str = "",
) -> tuple[str, str | None]:
    """
    Build the English comment using the exact same format as the Sinhala post:
    Bold title, body paragraphs, category hashtag, and channel username.
    Returns (caption, continuation).
    """
    caption, continuation, _, _, _, _, _, _ = build_post_content(item, lang="en", channel_tag=channel_tag)
    return caption, continuation


def post_discussion_comments(
    bot_token: str,
    chat_id: str,
    main_message_id: int | None,
    item: dict,
    share_url: str = "",
    cover_url: str | None = None,
    channel_tag: str = "@HelakuruEsanaNews",
) -> tuple[int | None, int | None, str, None, None]:
    """
    Post English translation with cover image (same format as Sinhala post)
    inside the channel post's Discussion Group thread.

    Returns:
        (disc_chat_id, comment_msg_id, comment_hash, None, None)
    """
    if not channel_tag:
        channel_tag = chat_id if str(chat_id).startswith("@") else "@HelakuruEsanaNews"

    eng_caption, eng_cont = build_english_comment(item, channel_tag=channel_tag)
    full_comment = f"{eng_caption}\n\n{eng_cont}" if eng_cont else eng_caption
    comment_hash = hashlib.sha256(full_comment.encode("utf-8")).hexdigest()

    if not main_message_id:
        return None, None, comment_hash, None, None

    if not cover_url:
        cover_url = (item.get("cover") or item.get("thumb") or "").strip()

    disc_chat_id, disc_msg_id = find_discussion_thread_message(bot_token, chat_id, main_message_id)
    comment_msg_id = None

    if disc_chat_id and disc_msg_id:
        # English translation comment with image + Esana link
        try:
            if cover_url:
                try:
                    res2 = tg_send_photo(
                        bot_token=bot_token,
                        chat_id=str(disc_chat_id),
                        image_url=cover_url,
                        caption=eng_caption,
                        parse_mode="HTML",
                        reply_to_message_id=disc_msg_id,
                    )
                except Exception as img_exc:
                    logger.warning("Failed to send English photo comment, falling back to text: %s", img_exc)
                    res2 = tg_api_request(
                        bot_token,
                        "sendMessage",
                        {
                            "chat_id": disc_chat_id,
                            "text": full_comment,
                            "parse_mode": "HTML",
                            "reply_to_message_id": disc_msg_id,
                            "disable_web_page_preview": True,
                        },
                    )
            else:
                res2 = tg_api_request(
                    bot_token,
                    "sendMessage",
                    {
                        "chat_id": disc_chat_id,
                        "text": full_comment,
                        "parse_mode": "HTML",
                        "reply_to_message_id": disc_msg_id,
                        "disable_web_page_preview": True,
                    },
                )
            comment_msg_id = (res2.get("result") or {}).get("message_id")
            logger.info(
                "Posted English translation comment with image (disc_msg_id=%s, comment_msg_id=%s)",
                disc_msg_id,
                comment_msg_id,
            )
            if eng_cont and comment_msg_id:
                time.sleep(0.8)
                tg_api_request(
                    bot_token,
                    "sendMessage",
                    {
                        "chat_id": disc_chat_id,
                        "text": eng_cont,
                        "parse_mode": "HTML",
                        "reply_to_message_id": comment_msg_id,
                        "disable_web_page_preview": True,
                    },
                )
        except Exception as exc:
            logger.warning("Failed to post English comment in discussion group: %s", exc)

        return disc_chat_id, comment_msg_id, comment_hash, None, None
    else:
        logger.warning(
            "Could not post comments for message_id=%s: bot did not receive the auto-forwarded discussion message.",
            main_message_id,
        )
    return None, None, comment_hash, None, None


def send_news_to_telegram(
    bot_token: str,
    chat_id: str,
    item: dict,
    lang: str = "si",
) -> tuple[int | None, int | None, str, list[str], int | None, int | None, int | None, int | None, str, int | None, str]:
    """
    Send the main news cover image with the news text as caption,
    followed by the discussion comment (English translation + link),
    continuation text (if >1024 chars), additional article images (grouped as album if 2+),
    voice audio message (if category 3 / voice news), and timestamp message.

    Returns:
        (main_message_id, continuation_msg_id, content_hash, extra_images, voice_msg_id, timestamp_msg_id,
         disc_chat_id, comment_msg_id, comment_hash, rx_msg_id, rx_hash)
    """
    channel_tag = chat_id if str(chat_id).startswith("@") else "@HelakuruEsanaNews"
    (
        caption,
        continuation,
        cover_url,
        extra_images,
        voice_url,
        voice_keys,
        share_url,
        content_hash,
    ) = build_post_content(item, lang=lang, channel_tag=channel_tag)

    main_message_id = None
    if cover_url:
        try:
            res = tg_send_photo(
                bot_token=bot_token,
                chat_id=chat_id,
                image_url=cover_url,
                caption=caption,
                parse_mode="HTML",
            )
            main_message_id = (res.get("result") or {}).get("message_id")
        except Exception as exc:
            logger.warning("Failed to send photo for news #%s, falling back to text: %s", item.get("id"), exc)
            res = tg_api_request(
                bot_token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": caption,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
            )
            main_message_id = (res.get("result") or {}).get("message_id")
    else:
        res = tg_api_request(
            bot_token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )
        main_message_id = (res.get("result") or {}).get("message_id")

    disc_chat_id, comment_msg_id, comment_hash, rx_msg_id, rx_hash = post_discussion_comments(
        bot_token, chat_id, main_message_id, item, share_url, cover_url=cover_url, channel_tag=channel_tag
    )

    continuation_msg_id = None
    if continuation:
        time.sleep(1.0)
        cont_payload: dict = {
            "chat_id": chat_id,
            "text": continuation,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if main_message_id:
            cont_payload["reply_to_message_id"] = main_message_id
        cont_res = tg_api_request(bot_token, "sendMessage", cont_payload)
        continuation_msg_id = (cont_res.get("result") or {}).get("message_id")

    send_extra_images(bot_token, chat_id, extra_images, main_message_id, item.get("id"))

    voice_msg_id = None
    timestamp_msg_id = None
    if voice_url and main_message_id:
        raw_title = item.get("titleSi") or item.get("titleEn") or ""
        voice_caption = format_voice_timestamps_caption(voice_keys, channel_tag=channel_tag)
        voice_msg_id = send_voice_audio(
            bot_token,
            chat_id,
            voice_url,
            reply_to_message_id=main_message_id,
            lang=lang,
            title=raw_title,
            caption=voice_caption,
        )

    return (
        main_message_id,
        continuation_msg_id,
        content_hash,
        extra_images,
        voice_msg_id,
        timestamp_msg_id,
        disc_chat_id,
        comment_msg_id,
        comment_hash,
        rx_msg_id,
        rx_hash,
    )


def update_existing_post_if_changed(
    state: StateDB,
    record: dict,
    bot_token: str,
    chat_id: str,
    item: dict,
    lang: str = "si",
    dry_run: bool = False,
) -> bool:
    """
    Check if an already-posted news item was edited on Esana;
    if so, edit the Telegram caption/message, send any newly added extra images,
    and send any missing voice audio message.
    """
    news_id = int(item["id"])
    channel_tag = chat_id if str(chat_id).startswith("@") else "@HelakuruEsanaNews"
    caption, continuation, cover_url, extra_images, voice_url, voice_keys, share_url, new_hash = build_post_content(
        item, lang=lang, channel_tag=channel_tag
    )
    eng_caption, eng_cont = build_english_comment(item, channel_tag=channel_tag)
    full_comment = f"{eng_caption}\n\n{eng_cont}" if eng_cont else eng_caption
    new_comment_hash = hashlib.sha256(full_comment.encode("utf-8")).hexdigest()

    old_hash = record.get("content_hash")
    msg_id = record.get("message_id")
    cont_msg_id = record.get("continuation_msg_id")
    old_extra_images = record.get("extra_images") or []
    disc_chat_id = record.get("disc_chat_id")
    comment_msg_id = record.get("comment_msg_id")
    old_comment_hash = record.get("comment_hash")
    last_synced_at = float(record.get("comment_synced_at") or 0.0)
    old_voice_msg_id = record.get("voice_msg_id")
    old_timestamp_msg_id = record.get("timestamp_msg_id")
    voice_msg_id = old_voice_msg_id
    timestamp_msg_id = old_timestamp_msg_id
    now_ts = time.time()

    if not old_hash:
        if not dry_run:
            title = item.get("titleSi") or item.get("titleEn") or f"#{news_id}"
            state.save_posted(
                news_id=news_id,
                title=title,
                published=item.get("published", ""),
                message_id=msg_id,
                continuation_msg_id=cont_msg_id,
                content_hash=new_hash,
                extra_images=extra_images,
                disc_chat_id=disc_chat_id,
                comment_msg_id=comment_msg_id,
                comment_hash=new_comment_hash,
                comment_synced_at=now_ts,
                voice_msg_id=voice_msg_id,
                voice_url=voice_url,
                timestamp_msg_id=timestamp_msg_id,
            )
        return False

    new_extra = [img for img in extra_images if img not in old_extra_images]
    main_changed = bool(msg_id and old_hash != new_hash)
    comment_changed = bool(
        disc_chat_id
        and comment_msg_id
        and old_comment_hash != new_comment_hash
        and ((now_ts - last_synced_at >= 120.0) or main_changed)
    )
    voice_missing = bool(voice_url and msg_id and not old_voice_msg_id)

    if not main_changed and not new_extra and not comment_changed and not voice_missing:
        return False

    if dry_run:
        print(f"[DRY RUN] Detected update on Esana for already-posted news #{news_id} (msg_id={msg_id})")
        return True

    # 1. Main post content changed on Esana
    if main_changed:
        try:
            if cover_url:
                tg_api_request(
                    bot_token,
                    "editMessageCaption",
                    {
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                )
            else:
                tg_api_request(
                    bot_token,
                    "editMessageText",
                    {
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": caption,
                        "parse_mode": "HTML",
                    },
                )
            logger.info("Auto-edited main post for updated news #%d (message_id=%s)", news_id, msg_id)
        except Exception as exc:
            if "message is not modified" not in str(exc):
                logger.warning("Failed to edit main message for news #%d: %s", news_id, exc)

        if continuation:
            try:
                if cont_msg_id:
                    tg_api_request(
                        bot_token,
                        "editMessageText",
                        {
                            "chat_id": chat_id,
                            "message_id": cont_msg_id,
                            "text": continuation,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                    )
                else:
                    cont_res = tg_api_request(
                        bot_token,
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": continuation,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                            "reply_to_message_id": msg_id,
                        },
                    )
                    cont_msg_id = (cont_res.get("result") or {}).get("message_id")
            except Exception as exc:
                if "message is not modified" not in str(exc):
                    logger.warning("Failed to update continuation message for news #%d: %s", news_id, exc)

    # 2. English translation / link changed
    if comment_changed:
        try:
            if cover_url:
                try:
                    tg_api_request(
                        bot_token,
                        "editMessageCaption",
                        {
                            "chat_id": disc_chat_id,
                            "message_id": comment_msg_id,
                            "caption": eng_caption,
                            "parse_mode": "HTML",
                        },
                    )
                except Exception as exc:
                    if "there is no caption in the message to edit" in str(exc):
                        tg_api_request(
                            bot_token,
                            "editMessageText",
                            {
                                "chat_id": disc_chat_id,
                                "message_id": comment_msg_id,
                                "text": full_comment,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True,
                            },
                        )
                    elif "message is not modified" not in str(exc):
                        raise
            else:
                tg_api_request(
                    bot_token,
                    "editMessageText",
                    {
                        "chat_id": disc_chat_id,
                        "message_id": comment_msg_id,
                        "text": full_comment,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            last_synced_at = now_ts
            logger.info("Updated English comment for news #%d (comment_msg_id=%s)", news_id, comment_msg_id)
        except Exception as exc:
            if "message is not modified" not in str(exc):
                logger.warning("Failed to edit comment for news #%d: %s", news_id, exc)

    if new_extra and msg_id:
        send_extra_images(bot_token, chat_id, new_extra, msg_id, news_id)
        logger.info("Sent %d newly added extra image(s) for news #%d", len(new_extra), news_id)

    # 3. Missing voice note
    if voice_missing:
        raw_title = item.get("titleSi") or item.get("titleEn") or ""
        voice_caption = format_voice_timestamps_caption(voice_keys, channel_tag=channel_tag)
        voice_msg_id = send_voice_audio(
            bot_token,
            chat_id,
            voice_url,
            reply_to_message_id=msg_id,
            lang=lang,
            title=raw_title,
            caption=voice_caption,
        )
        if voice_msg_id:
            logger.info("Sent missing voice note for news #%d (voice_msg_id=%s)", news_id, voice_msg_id)

    title = item.get("titleSi") or item.get("titleEn") or f"#{news_id}"
    state.save_posted(
        news_id=news_id,
        title=title,
        published=item.get("published", ""),
        message_id=msg_id,
        continuation_msg_id=cont_msg_id,
        content_hash=new_hash,
        extra_images=extra_images,
        disc_chat_id=disc_chat_id,
        comment_msg_id=comment_msg_id,
        comment_hash=new_comment_hash,
        comment_synced_at=last_synced_at,
        voice_msg_id=voice_msg_id,
        voice_url=voice_url,
        timestamp_msg_id=timestamp_msg_id,
    )
    return True


def poll_once(
    state: StateDB,
    bot_token: str,
    chat_id: str,
    lang: str = "si",
    category: int = 1,
    initial_post_limit: int = 1,
    dry_run: bool = False,
) -> int:
    """Fetch Esana news, post unseen items, and auto-edit updated items. Returns number of items sent/updated."""
    items = fetch_esana_news(category=category)
    if not items:
        logger.warning("No news items returned from Esana.")
        return 0

    channel_tag = chat_id if str(chat_id).startswith("@") else "@HelakuruEsanaNews"
    items_sorted = sorted(items, key=lambda x: int(x.get("id", 0)))

    if state.is_empty():
        to_skip = items_sorted[:-initial_post_limit] if initial_post_limit > 0 else items_sorted
        for old_item in to_skip:
            oid = int(old_item["id"])
            title = old_item.get("titleSi") or old_item.get("titleEn") or ""
            _, _, _, extra_imgs, _, _, _, chash = build_post_content(old_item, lang=lang, channel_tag=channel_tag)
            if not dry_run:
                state.save_posted(
                    news_id=oid,
                    title=title,
                    published=old_item.get("published", ""),
                    content_hash=chash,
                    extra_images=extra_imgs,
                )
        logger.info(
            "Initialized state DB: marked %d older items as seen, will post latest %d item(s).",
            len(to_skip),
            len(items_sorted) - len(to_skip),
        )
        items_to_post = items_sorted[len(to_skip):]
        items_to_check_edits = []
    else:
        items_to_post = []
        items_to_check_edits = []
        for it in items_sorted:
            if state.is_posted(int(it["id"])):
                items_to_check_edits.append(it)
            else:
                items_to_post.append(it)

    processed_count = 0

    # Check already-posted recent items for Esana edits/updates
    for item in items_to_check_edits:
        rec = state.get_record(int(item["id"]))
        if rec and rec.get("message_id"):
            if update_existing_post_if_changed(
                state, rec, bot_token, chat_id, item, lang=lang, dry_run=dry_run
            ):
                processed_count += 1
                if not dry_run:
                    time.sleep(1.0)

    # Post new items
    for item in items_to_post:
        news_id = int(item["id"])
        title = item.get("titleSi") or item.get("titleEn") or f"#{news_id}"
        published = item.get("published", "")

        if dry_run:
            caption, continuation, cover_url, extra_images, voice_url, voice_keys, share_url, _hash = build_post_content(
                item, lang=lang, channel_tag=channel_tag
            )
            print("=" * 60)
            print(f"[DRY RUN] News ID: {news_id}")
            print(f"1) Main Photo (sendPhoto): {cover_url}")
            print("-" * 60)
            print(caption)
            step = 2
            if continuation:
                print("-" * 60)
                print(f"{step}) Continuation Reply (sendMessage):")
                print(continuation)
                step += 1
            if extra_images:
                print("-" * 60)
                mode = "sendMediaGroup Album" if len(extra_images) >= 2 else "sendPhoto"
                print(f"{step}) Extra Images ({mode}, {len(extra_images)} photo(s)):")
                for img_url in extra_images:
                    print(f"   - {img_url}")
                step += 1
            if voice_url:
                print("-" * 60)
                print(f"{step}) Voice Clip (sendVoice with Timestamps Caption): {voice_url}")
                if voice_keys:
                    voice_caption = format_voice_timestamps_caption(voice_keys, channel_tag=channel_tag)
                    print("   [Voice Caption]:")
                    for vline in (voice_caption or "").split("\n"):
                        print(f"     {vline}")
                step += 1
            eng_caption, eng_cont = build_english_comment(item, channel_tag=channel_tag)
            print("-" * 60)
            print(f"{step}) Comment (English Translation + Photo):")
            if cover_url:
                print(f"Photo: {cover_url}")
            print(eng_caption)
            if eng_cont:
                print("-" * 30)
                print("Continuation:")
                print(eng_cont)
            print("=" * 60)
            processed_count += 1
            continue

        try:
            caption, continuation, cover_url, extra_images, voice_url, voice_keys, share_url, _hash = build_post_content(
                item, lang=lang, channel_tag=channel_tag
            )
            (
                msg_id,
                cont_id,
                chash,
                extra_imgs,
                voice_mid,
                ts_mid,
                disc_cid,
                comm_id,
                comm_hash,
                rx_msg_id,
                rx_hash,
            ) = send_news_to_telegram(bot_token, chat_id, item, lang=lang)
            state.save_posted(
                news_id=news_id,
                title=title,
                published=published,
                message_id=msg_id,
                continuation_msg_id=cont_id,
                content_hash=chash,
                extra_images=extra_imgs,
                disc_chat_id=disc_cid,
                comment_msg_id=comm_id,
                comment_hash=comm_hash,
                comment_synced_at=time.time(),
                voice_msg_id=voice_mid,
                voice_url=voice_url,
                timestamp_msg_id=ts_mid,
            )
            processed_count += 1
            logger.info("Posted news #%d (message_id=%s): %s", news_id, msg_id, title)
            time.sleep(1.5)
        except Exception as post_err:
            logger.error("Failed to post news #%d: %s", news_id, post_err, exc_info=True)

    return processed_count


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")

    parser = argparse.ArgumentParser(description="Helakuru Esana -> Telegram RSS Bot")
    parser.add_argument("--token", default=os.environ.get("TG_BOT_TOKEN", ""), help="Telegram Bot Token (or set TG_BOT_TOKEN in .env)")
    parser.add_argument("--chat-id", default=os.environ.get("TG_CHAT_ID", ""), help="Telegram Chat/Channel ID (or set TG_CHAT_ID in .env)")
    parser.add_argument("--lang", choices=["si", "en"], default=os.environ.get("ESANA_LANG", "si"), help="Language: 'si' or 'en'")
    parser.add_argument("--category", type=int, default=int(os.environ.get("ESANA_CATEGORY", "1")), help="Esana category ID (1=All)")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("POLL_INTERVAL", "30")), help="Polling interval in seconds (default: 30)")
    parser.add_argument("--initial-limit", type=int, default=int(os.environ.get("INITIAL_POST_LIMIT", "1")), help="How many latest news items to send on first startup (default: 1)")
    parser.add_argument("--db", default=os.environ.get("STATE_DB_PATH", str(base_dir / "esana_state.db")), help="Path to SQLite state database")
    parser.add_argument("--once", action="store_true", help="Run a single poll cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Preview formatted Telegram posts without sending or modifying DB")
    args = parser.parse_args()

    if not args.dry_run and (not args.token or not args.chat_id):
        parser.error("TG_BOT_TOKEN and TG_CHAT_ID are required (set them in .env or pass --token and --chat-id). Use --dry-run to test without credentials.")

    state = StateDB(Path(args.db))
    try:
        if args.dry_run or args.once:
            count = poll_once(
                state=state,
                bot_token=args.token,
                chat_id=args.chat_id,
                lang=args.lang,
                category=args.category,
                initial_post_limit=args.initial_limit,
                dry_run=args.dry_run,
            )
            logger.info("Poll complete (%d item(s) processed).", count)
            return

        logger.info(
            "Starting Helakuru Esana Telegram Bot (chat_id=%s, lang=%s, interval=%ds)",
            args.chat_id,
            args.lang,
            args.interval,
        )
        while True:
            try:
                poll_once(
                    state=state,
                    bot_token=args.token,
                    chat_id=args.chat_id,
                    lang=args.lang,
                    category=args.category,
                    initial_post_limit=args.initial_limit,
                    dry_run=False,
                )
            except Exception as exc:
                logger.error("Error during poll cycle: %s", exc)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        state.close()


if __name__ == "__main__":
    main()
