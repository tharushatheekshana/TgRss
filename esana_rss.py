#!/usr/bin/env python3
"""Fetch news from Helakuru Esana (https://www.helakuru.lk/esana) and generate an RSS 2.0 feed."""

import argparse
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
import html
import http.cookiejar
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
import sys
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

ESANA_URL = "https://www.helakuru.lk/esana"
ESANA_API_URL = "https://www.helakuru.lk/esana/load-initial-data"
SL_TZ = timezone(timedelta(hours=5, minutes=30))


def fetch_esana_news(category: int = 1, limit: str = "") -> list[dict]:
    """Fetch news JSON list from Helakuru Esana using a session with CSRF token."""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    ]

    page_html = opener.open(ESANA_URL, timeout=15).read().decode("utf-8")
    match = re.search(r"var\s+csrfHash\s*=\s*'([^']+)';", page_html)
    if not match:
        raise RuntimeError("Could not find csrfHash on https://www.helakuru.lk/esana")
    csrf_hash = match.group(1)

    payload = urllib.parse.urlencode({
        "newsLimit": limit,
        "esanaWidget": "false",
        "csrf": csrf_hash,
        "category": category,
    }).encode("utf-8")

    req = urllib.request.Request(
        ESANA_API_URL,
        data=payload,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": ESANA_URL,
        },
    )
    resp_bytes = opener.open(req, timeout=15).read()
    data = json.loads(resp_bytes.decode("utf-8"))
    if not data.get("STATUS"):
        raise RuntimeError("Esana API returned STATUS=False")

    news = (data.get("verticalNews") or {}).get("NEWS") or []
    if not news:
        fallback_req = urllib.request.Request(
            "https://www.helakuru.lk/esana/load",
            data=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": ESANA_URL,
            },
        )
        fb_data = json.loads(opener.open(fallback_req, timeout=15).read().decode("utf-8"))
        news = fb_data.get("NEWS") or []

    return news


def format_transcript_keys(keys: list[dict]) -> list[str]:
    """Group subtitle keys into readable paragraph chunks."""
    if not keys:
        return []
    chunks: list[str] = []
    current_sentences: list[str] = []
    current_len = 0

    for k in keys:
        if not isinstance(k, dict):
            continue
        t = (k.get("text") or "").strip()
        if not t:
            continue
        current_sentences.append(t)
        current_len += len(t)
        if current_len >= 180 and t.endswith((".", "!", "?", "...", "—")):
            chunks.append(" ".join(current_sentences))
            current_sentences = []
            current_len = 0

    if current_sentences:
        chunks.append(" ".join(current_sentences))

    result = []
    for c in chunks:
        cleaned = re.sub(r"\s+", " ", c).strip()
        if cleaned:
            result.append(cleaned)
    return result


def render_content_blocks(blocks: list[dict] | None) -> str:
    """Convert Esana structured content blocks into HTML."""
    if not blocks:
        return ""
    parts = []
    for block in blocks:
        btype = block.get("type")
        bdata = block.get("data") or ""
        if btype == "text":
            text = bdata.strip()
            if text:
                parts.append(f"<p>{html.escape(text)}</p>")
        elif btype == "image" and bdata:
            parts.append(f'<p><img src="{html.escape(bdata)}" alt="Esana Image" /></p>')
        elif btype in ("audio", "voice") and bdata:
            parts.append(f'<p><audio controls src="{html.escape(bdata)}"></audio></p>')
            keys = block.get("keys") or []
            if keys:
                paragraphs = format_transcript_keys(keys)
                for p in paragraphs:
                    parts.append(f"<p>{html.escape(p)}</p>")
        elif btype == "iframe" and bdata:
            parts.append(f'<p><iframe src="{html.escape(bdata)}"></iframe></p>')
    return "\n".join(parts)


def build_rss_xml(news_items: list[dict], lang: str = "si") -> str:
    """Build an RSS 2.0 XML string from Esana news items."""
    now_rfc822 = format_datetime(datetime.now(SL_TZ))
    channel_title = "හෙළකුරු එසැණ (Helakuru Esana)" if lang == "si" else "Helakuru Esana News"
    channel_desc = "Get Sri Lanka’s Breaking News instantly from Helakuru Esana."

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "  <channel>",
        f"    <title>{escape(channel_title)}</title>",
        f"    <link>{ESANA_URL}</link>",
        f"    <description>{escape(channel_desc)}</description>",
        f"    <language>{'si' if lang == 'si' else 'en'}</language>",
        f"    <lastBuildDate>{now_rfc822}</lastBuildDate>",
    ]

    for item in news_items:
        news_id = item.get("id")
        if lang == "en":
            title = item.get("titleEn") or item.get("titleSi") or f"News #{news_id}"
            blocks = item.get("contentEn") or item.get("contentSi") or []
        else:
            title = item.get("titleSi") or item.get("titleEn") or f"News #{news_id}"
            blocks = item.get("contentSi") or item.get("contentEn") or []

        link = item.get("share_url") or f"https://www.helakuru.lk/esana/news/{news_id}"
        thumb = item.get("cover") or item.get("thumb") or ""
        body_html = render_content_blocks(blocks)
        if thumb:
            body_html = f'<p><img src="{html.escape(thumb)}" alt="Cover" /></p>\n' + body_html

        pub_str = item.get("published", "")
        try:
            dt = datetime.strptime(pub_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SL_TZ)
            pub_date = format_datetime(dt)
        except ValueError:
            pub_date = now_rfc822

        lines.append("    <item>")
        lines.append(f"      <title>{escape(title)}</title>")
        lines.append(f"      <link>{escape(link)}</link>")
        lines.append(f'      <guid isPermaLink="true">{escape(link)}</guid>')
        lines.append(f"      <pubDate>{pub_date}</pubDate>")
        if thumb:
            lines.append(f'      <media:content url="{escape(thumb)}" medium="image" />')
        lines.append(f"      <description><![CDATA[{body_html}]]></description>")
        lines.append("    </item>")

    lines.append("  </channel>")
    lines.append("</rss>")
    return "\n".join(lines) + "\n"


def serve_rss(host: str, port: int, default_lang: str, category: int) -> None:
    """Run a lightweight HTTP server serving the live Esana RSS feed."""

    class RSSHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            lang = qs.get("lang", [default_lang])[0]
            cat = int(qs.get("category", [str(category)])[0])
            try:
                items = fetch_esana_news(category=cat)
                rss_xml = build_rss_xml(items, lang=lang).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(rss_xml)))
                self.end_headers()
                self.wfile.write(rss_xml)
            except Exception as exc:
                msg = f"Error fetching Esana feed: {exc}\n".encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(msg)

    server = HTTPServer((host, port), RSSHandler)
    print(f"Serving Helakuru Esana RSS at http://{host}:{port}/rss (use ?lang=si or ?lang=en)", file=sys.stderr)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate or serve an RSS 2.0 feed from Helakuru Esana.")
    parser.add_argument("-o", "--output", help="Output RSS XML file path (prints to stdout if omitted)")
    parser.add_argument("-l", "--lang", choices=["si", "en"], default="si", help="Language: 'si' (Sinhala) or 'en' (English)")
    parser.add_argument("-c", "--category", type=int, default=1, help="Category ID (1=All, 2=Incidents, 3=Voice, 4=Announcements, 5=Statements)")
    parser.add_argument("--serve", action="store_true", help="Start a local HTTP server to serve the RSS feed live")
    parser.add_argument("--host", default="127.0.0.1", help="Host for --serve (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Port for --serve (default: 8080)")
    args = parser.parse_args()

    if args.serve:
        serve_rss(args.host, args.port, args.lang, args.category)
        return

    items = fetch_esana_news(category=args.category)
    rss_xml = build_rss_xml(items, lang=args.lang)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(rss_xml)
        print(f"Wrote {len(items)} items to {args.output}")
    else:
        sys.stdout.write(rss_xml)


if __name__ == "__main__":
    main()
