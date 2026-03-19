"""
pravo_scraper.py — Парсер Национального правового интернет-портала Республики Беларусь
========================================================================================

Поддерживает два источника:
  1. Прямая ссылка на документ:
       https://pravo.by/document/?guid=3871&p0=H12300270
  2. Поиск по ключевому слову / фильтр по типу:
       https://pravo.by/document/?guid=3871&sortStatus=2&selectedSearchType=ByRequisites...

Архитектура:
  PravoScraper.fetch_document(url)   → dict  (один документ)
  PravoScraper.search(query, ...)    → list  (список URL + мета)
  PravoScraper.scrape_and_save(...)  → int   (сохранённых в БД)

CLI:
  python pravo_scraper.py --query "трудовой кодекс" --limit 10
  python pravo_scraper.py --urls "https://pravo.by/document/?guid=3871&p0=H12300270"
  python pravo_scraper.py --categories laws decrees --limit 50

Опции вежливого парсинга:
  --delay 2.0     пауза между запросами (сек)
  --timeout 15    таймаут HTTP
  --retries 3     повторов при ошибке
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("pravo_scraper")

# ─── Constants ────────────────────────────────────────────────────────────────

BASE   = "https://pravo.by"
UA     = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# pravo.by document URL patterns
# Direct doc: /document/?guid=3871&p0=<code>
# Search:     /document/?guid=3871&sortStatus=2&...
DOC_BASE   = f"{BASE}/document/"
DOC_GUID   = "3871"  # fixed GUID for main NPA section

# Human-readable category → URL fragment mapping
# (top-level НПА categories on pravo.by)
CATEGORIES: dict[str, dict] = {
    "laws": {
        "label": "Законы",
        "url": f"{BASE}/document/?guid=3871&sortStatus=2&selectedSearchType=ByRequisites"
               "&docCategory=1",
    },
    "decrees": {
        "label": "Декреты Президента",
        "url": f"{BASE}/document/?guid=3871&sortStatus=2&selectedSearchType=ByRequisites"
               "&docCategory=4",
    },
    "edicts": {
        "label": "Указы Президента",
        "url": f"{BASE}/document/?guid=3871&sortStatus=2&selectedSearchType=ByRequisites"
               "&docCategory=5",
    },
    "resolutions": {
        "label": "Постановления Совета Министров",
        "url": f"{BASE}/document/?guid=3871&sortStatus=2&selectedSearchType=ByRequisites"
               "&docCategory=8",
    },
    "ministry": {
        "label": "НПА министерств",
        "url": f"{BASE}/document/?guid=3871&sortStatus=2&selectedSearchType=ByRequisites"
               "&docCategory=13",
    },
    "all": {
        "label": "Все НПА",
        "url": f"{BASE}/document/?guid=3871&sortStatus=2",
    },
}

# CSS-like selector fallbacks for document body text
_DOC_BODY_SELECTORS = [
    "div.npa-text",
    "div.document-text",
    "div.document-body",
    "div.doc-text",
    "div#documentBody",
    "div#doc-body",
    "div.text-document",
    "article.document",
    "div.main-content",
    "div[class*='document']",
    "div[class*='npa']",
    "div[class*='text']",
    "main",
    "article",
]

_DOC_TITLE_SELECTORS = [
    "h1.document-title",
    "h1.npa-title",
    "h1#documentTitle",
    "h1",
    ".document-name",
    ".npa-name",
    "title",
]

_LINK_PATTERNS = [
    r"/document/\?guid=3871&p0=[A-Za-z0-9]+",
    r"/document/\?[^\"'\s]*p0=[A-Za-z0-9]+",
]


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

class ScraperError(Exception):
    pass


def _get(url: str, timeout: int = 15, retries: int = 3, delay: float = 1.0) -> bytes:
    """HTTP GET with retry logic and polite delay."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        },
    )
    last_err: Exception = ScraperError("no attempt")
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            time.sleep(delay)
            return data
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code == 404:
                raise ScraperError(f"404 Not Found: {url}") from exc
            if exc.code == 429:
                wait = float(exc.headers.get("Retry-After", delay * 3))
                logger.warning("429 rate-limit, wait=%.0fs", wait)
                time.sleep(wait)
            else:
                logger.warning("HTTP %d attempt=%d/%d url=%s", exc.code, attempt, retries, url)
                time.sleep(delay * attempt)
        except Exception as exc:
            last_err = exc
            logger.warning("fetch error attempt=%d/%d url=%s: %s", attempt, retries, url, exc)
            time.sleep(delay * attempt)
    raise ScraperError(f"All retries failed for {url}: {last_err}") from last_err


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ─── HTML parsing (manual, no BS4 required) ───────────────────────────────────

def _strip_tags(html: str) -> str:
    """Remove HTML tags and decode common entities."""
    # Remove script/style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove comments
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    # Replace block elements with newlines
    html = re.sub(
        r"<(p|div|li|tr|h[1-6]|br|article|section|header|footer)[^>]*>",
        "\n", html, flags=re.IGNORECASE
    )
    # Remove remaining tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode entities
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    html = html.replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    html = re.sub(r"&[a-z]+;", " ", html)
    html = re.sub(r"&#\d+;", " ", html)
    # Normalise whitespace
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _extract_between(html: str, selector_hint: str) -> str:
    """
    Try to extract content of a given tag/class from raw HTML.
    selector_hint examples:  "div.npa-text",  "h1",  "div#documentBody"
    """
    # Parse selector hint
    tag = re.match(r"[a-z0-9]+", selector_hint, re.IGNORECASE)
    if not tag:
        return ""
    tag_name = tag.group(0)
    cls_match = re.search(r"\.([\w-]+)", selector_hint)
    id_match  = re.search(r"#([\w-]+)", selector_hint)
    attr_match = re.search(r"\[([^\]]+)\]", selector_hint)

    # Build attribute pattern
    if cls_match:
        attr_pat = rf'class="[^"]*{re.escape(cls_match.group(1))}[^"]*"'
    elif id_match:
        attr_pat = rf'id="{re.escape(id_match.group(1))}"'
    elif attr_match:
        attr_pat = re.escape(attr_match.group(1))
    else:
        attr_pat = None

    # Find opening tag
    if attr_pat:
        open_re = re.compile(
            rf"<{tag_name}[^>]*{attr_pat}[^>]*>", re.IGNORECASE
        )
    else:
        open_re = re.compile(rf"<{tag_name}(?:\s[^>]*)?>", re.IGNORECASE)

    m = open_re.search(html)
    if not m:
        return ""

    # Extract until matching closing tag (counting nesting)
    start = m.end()
    depth = 1
    pos = start
    close_tag = f"</{tag_name}>"
    open_tag_re = re.compile(rf"<{tag_name}(?:\s[^>]*)?>", re.IGNORECASE)
    close_tag_re = re.compile(rf"</{tag_name}>", re.IGNORECASE)

    while depth > 0 and pos < len(html):
        next_open  = open_tag_re.search(html, pos)
        next_close = close_tag_re.search(html, pos)
        if not next_close:
            break
        if next_open and next_open.start() < next_close.start():
            depth += 1
            pos = next_open.end()
        else:
            depth -= 1
            pos = next_close.end()
            if depth == 0:
                return html[start:next_close.start()]
    return html[start:pos]


def _extract_title(html: str) -> str:
    for sel in _DOC_TITLE_SELECTORS:
        chunk = _extract_between(html, sel)
        if chunk:
            title = _strip_tags(chunk).strip()[:300]
            if len(title) > 5:
                return title
    # fallback: <title>
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if m:
        return _strip_tags(m.group(1)).strip()[:300]
    return ""


def _extract_body(html: str) -> str:
    """Try each body selector, return the longest match."""
    best = ""
    for sel in _DOC_BODY_SELECTORS:
        chunk = _extract_between(html, sel)
        if chunk and len(chunk) > len(best):
            best = chunk
    if best:
        return _strip_tags(best)
    # Last resort: strip entire <body>
    body = _extract_between(html, "body")
    if body:
        return _strip_tags(body)
    return _strip_tags(html)


def _extract_doc_links(html: str, base_url: str = BASE) -> list[str]:
    """Extract all unique document URLs from a list/search page."""
    links: set[str] = set()
    for pat in _LINK_PATTERNS:
        for m in re.finditer(pat, html):
            href = m.group(0)
            if href.startswith("http"):
                links.add(href)
            else:
                links.add(base_url + href)
    return sorted(links)


def _extract_next_page(html: str, current_url: str) -> str | None:
    """Try to find pagination 'next' URL."""
    # Look for rel="next" or Следующая / > / »
    patterns = [
        r'<a[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']',
        r'href=["\']([^"\']+)["\'][^>]*rel=["\']next["\']',
        r'<a[^>]+href=["\']([^"\'?][^"\']*)["\'][^>]*>(?:Следующая|&gt;&gt;|»|\>)',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            href = m.group(1)
            if href.startswith("http"):
                return href
            if href.startswith("/"):
                return BASE + href
            # relative to current page
            return urllib.parse.urljoin(current_url, href)
    # pravo.by uses ?page=N pagination
    m_page = re.search(r"[?&]page=(\d+)", current_url)
    cur_pg = int(m_page.group(1)) if m_page else 1
    # check if there's a page+1 link in HTML
    next_pg = str(cur_pg + 1)
    if f"page={next_pg}" in html:
        if m_page:
            return current_url.replace(f"page={cur_pg}", f"page={next_pg}")
        sep = "&" if "?" in current_url else "?"
        return current_url + sep + f"page={next_pg}"
    return None


# ─── Search URL builder ───────────────────────────────────────────────────────

def build_search_url(
    query: str = "",
    category: str = "all",
    page: int = 1,
    date_from: str = "",   # DD.MM.YYYY
    date_to: str = "",
) -> str:
    base = CATEGORIES.get(category, CATEGORIES["all"])["url"]
    params: dict[str, str] = {}
    if query:
        params["searchText"] = query
        params["selectedSearchType"] = "ByText"
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to
    if page > 1:
        params["page"] = str(page)
    if params:
        sep = "&" if "?" in base else "?"
        base = base + sep + urllib.parse.urlencode(params, encoding="utf-8")
    return base


# ─── Core scraper ─────────────────────────────────────────────────────────────

class PravoScraper:
    def __init__(
        self,
        delay: float = 2.0,
        timeout: int = 15,
        retries: int = 3,
        db_path: str | None = None,
    ):
        self.delay   = delay
        self.timeout = timeout
        self.retries = retries
        self.db_path = db_path or os.environ.get(
            "DB_PATH",
            str(Path(__file__).parent / "data" / "lexanaliz.db")
        )
        self._db = None

    # ── DB integration ────────────────────────────────────────────────────────

    def _get_db(self):
        """Lazy-import DB module. Works from any working directory."""
        if self._db is None:
            # Walk up from this file to find project root (directory that has backend/)
            _here = Path(__file__).resolve()
            for parent in (_here.parent, _here.parent.parent, _here.parent.parent.parent):
                if (parent / "backend" / "db" / "database.py").exists():
                    if str(parent) not in sys.path:
                        sys.path.insert(0, str(parent))
                    break
            try:
                from backend.db.database import init_db, save_document
                self._db = (init_db, save_document)
            except ImportError as exc:
                logger.warning("DB module not found (%s) — documents won't be saved", exc)
                self._db = (None, None)
        return self._db

    def _save_to_db(self, doc: dict) -> str | None:
        """Save scraped document to DB. Returns doc_id or None."""
        try:
            init_db, save_document = self._get_db()
            if save_document is None:
                return None
            init_db()
            plain_text = doc["plain_text"]
            paragraphs = [l.strip() for l in plain_text.splitlines() if l.strip()]
            # Build chunks (inline minimal chunker to avoid import issues)
            chunks = _inline_chunks(paragraphs)
            doc_id = save_document(
                filename   = doc["filename"],
                ext        = "html",
                data       = plain_text.encode("utf-8"),
                plain_text = plain_text,
                paragraphs = paragraphs,
                chunks     = chunks,
                source     = "pravo",
                title      = doc["title"],
            )
            return doc_id
        except Exception as exc:
            logger.error("DB save failed: %s", exc)
            return None

    # ── Single document ───────────────────────────────────────────────────────

    def fetch_document(self, url: str) -> dict:
        """
        Fetch and parse a single pravo.by document page.

        Returns:
            {
              url, title, filename, plain_text,
              char_count, para_count,
              metadata: { source_url, ... }
            }
        """
        logger.info("Fetching: %s", url)
        raw = _get(url, timeout=self.timeout, retries=self.retries, delay=self.delay)
        html = _decode(raw)

        title    = _extract_title(html)
        body_txt = _extract_body(html)

        if len(body_txt) < 50:
            raise ScraperError(f"Too little text extracted from {url} ({len(body_txt)} chars)")

        # Derive filename from URL p0 param or hash
        m = re.search(r"p0=([A-Za-z0-9]+)", url)
        doc_code  = m.group(1) if m else hashlib.md5(url.encode()).hexdigest()[:12]
        safe_title = re.sub(r"[^\w\s-]", "", (title or doc_code)[:60]).strip()
        filename  = f"{safe_title or doc_code}.html"

        paragraphs = [l.strip() for l in body_txt.splitlines() if l.strip()]

        return {
            "url":       url,
            "title":     title,
            "filename":  filename,
            "plain_text": body_txt,
            "char_count": len(body_txt),
            "para_count": len(paragraphs),
            "metadata": {
                "source_url": url,
                "doc_code":   doc_code,
                "scraped_at": int(time.time()),
            },
        }

    # ── Search / list crawl ───────────────────────────────────────────────────

    def iter_doc_urls(
        self,
        start_url: str,
        max_pages: int = 10,
        max_docs: int = 200,
    ) -> Iterator[str]:
        """
        Crawl a listing/search page and yield individual document URLs.
        Follows pagination up to max_pages.
        """
        seen_links: set[str] = set()
        url = start_url
        pages_fetched = 0

        while url and pages_fetched < max_pages and len(seen_links) < max_docs:
            logger.info("List page %d: %s", pages_fetched + 1, url)
            try:
                raw  = _get(url, timeout=self.timeout, retries=self.retries, delay=self.delay)
                html = _decode(raw)
            except ScraperError as exc:
                logger.error("List page fetch failed: %s", exc)
                break

            new_links = _extract_doc_links(html, BASE)
            # Exclude the current search/list URL itself
            new_links = [l for l in new_links if l != url and l not in seen_links]

            for link in new_links:
                if len(seen_links) >= max_docs:
                    break
                seen_links.add(link)
                yield link

            pages_fetched += 1
            next_url = _extract_next_page(html, url)
            if next_url == url:
                break
            url = next_url

        logger.info("Found %d document URLs across %d pages", len(seen_links), pages_fetched)

    def search(
        self,
        query: str = "",
        category: str = "all",
        max_docs: int = 50,
        max_pages: int = 5,
        date_from: str = "",
        date_to: str = "",
    ) -> list[str]:
        """Search pravo.by and return list of document URLs."""
        start = build_search_url(query, category, 1, date_from, date_to)
        return list(self.iter_doc_urls(start, max_pages=max_pages, max_docs=max_docs))

    # ── Bulk scrape + save ────────────────────────────────────────────────────

    def scrape_and_save(
        self,
        urls: list[str],
        on_progress: callable = None,
    ) -> dict:
        """
        Scrape each URL and save to DB.

        Returns:
            { saved: int, failed: int, skipped: int, docs: [meta...] }
        """
        saved = failed = skipped = 0
        docs_meta = []

        for i, url in enumerate(urls):
            if on_progress:
                on_progress(i, len(urls), url)
            try:
                doc    = self.fetch_document(url)
                doc_id = self._save_to_db(doc)
                if doc_id:
                    saved += 1
                    docs_meta.append({
                        "doc_id":  doc_id,
                        "title":   doc["title"],
                        "url":     url,
                        "chars":   doc["char_count"],
                    })
                    logger.info("✓ Saved: %s (%d chars)", doc["title"][:60], doc["char_count"])
                else:
                    skipped += 1
                    logger.warning("○ DB save skipped: %s", url)
            except ScraperError as exc:
                failed += 1
                logger.error("✗ Failed: %s → %s", url, exc)
            except Exception as exc:
                failed += 1
                logger.error("✗ Unexpected error for %s: %s", url, exc)

        return {
            "saved":   saved,
            "failed":  failed,
            "skipped": skipped,
            "total":   len(urls),
            "docs":    docs_meta,
        }

    def run(
        self,
        urls: list[str] | None = None,
        query: str = "",
        category: str = "all",
        max_docs: int = 50,
        max_pages: int = 5,
        date_from: str = "",
        date_to: str = "",
        on_progress: callable = None,
    ) -> dict:
        """
        Full pipeline: discover URLs (if not given) → scrape → save.
        """
        if not urls:
            logger.info("Discovering documents (query=%r category=%s limit=%d)…",
                        query, category, max_docs)
            urls = self.search(query, category, max_docs, max_pages, date_from, date_to)
            logger.info("Discovered %d URLs", len(urls))

        if not urls:
            return {"saved": 0, "failed": 0, "skipped": 0, "total": 0, "docs": [],
                    "error": "No document URLs found. pravo.by may block automated access "
                             "or the search returned no results."}

        return self.scrape_and_save(urls, on_progress)


# ─── Minimal inline chunker (no import needed) ────────────────────────────────

def _inline_chunks(paragraphs: list[str], chunk_max: int = 2000) -> list[dict]:
    chunks, buf, blen = [], [], 0
    for p in paragraphs:
        if buf and blen + len(p) > chunk_max:
            text = " ".join(buf)
            if len(text) >= 100:
                chunks.append({"index": len(chunks), "text": text, "para_count": len(buf)})
            buf, blen = [], 0
        buf.append(p)
        blen += len(p) + 1
    if buf:
        text = " ".join(buf)
        if len(text) >= 100:
            chunks.append({"index": len(chunks), "text": text, "para_count": len(buf)})
    return chunks


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Парсер pravo.by → БД ЛексАнализ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  # Скачать 20 законов
  python pravo_scraper.py --category laws --limit 20

  # Поиск по ключевому слову
  python pravo_scraper.py --query "трудовой кодекс" --limit 5

  # Прямые ссылки
  python pravo_scraper.py --urls \\
      "https://pravo.by/document/?guid=3871&p0=H12300270" \\
      "https://pravo.by/document/?guid=3871&p0=H11800130"

  # Законы за 2024 год
  python pravo_scraper.py --category laws --date-from 01.01.2024 --date-to 31.12.2024 --limit 30
        """,
    )
    parser.add_argument("--urls",      nargs="+", help="Прямые URL документов")
    parser.add_argument("--query",     default="", help="Поисковый запрос")
    parser.add_argument("--category",  default="all",
                        choices=list(CATEGORIES.keys()),
                        help="Категория НПА (default: all)")
    parser.add_argument("--limit",     type=int, default=50,
                        help="Максимум документов (default: 50)")
    parser.add_argument("--pages",     type=int, default=5,
                        help="Максимум страниц пагинации (default: 5)")
    parser.add_argument("--date-from", default="", dest="date_from",
                        help="Дата от DD.MM.YYYY")
    parser.add_argument("--date-to",   default="", dest="date_to",
                        help="Дата до DD.MM.YYYY")
    parser.add_argument("--delay",     type=float, default=2.0,
                        help="Задержка между запросами сек (default: 2.0)")
    parser.add_argument("--timeout",   type=int, default=15,
                        help="HTTP timeout сек (default: 15)")
    parser.add_argument("--retries",   type=int, default=3,
                        help="Повторов при ошибке (default: 3)")
    parser.add_argument("--db-path",   default="", dest="db_path",
                        help="Путь к SQLite БД (default: из ENV DB_PATH)")
    parser.add_argument("--output",    default="",
                        help="Сохранить результаты в JSON файл")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Только показать найденные URL, не парсить")
    parser.add_argument("--verbose",   action="store_true")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    scraper = PravoScraper(
        delay   = args.delay,
        timeout = args.timeout,
        retries = args.retries,
        db_path = args.db_path or None,
    )

    def progress(i: int, total: int, url: str) -> None:
        print(f"  [{i+1}/{total}] {url[:80]}", flush=True)

    if args.dry_run:
        # Just list URLs
        if args.urls:
            urls = args.urls
        else:
            urls = scraper.search(
                args.query, args.category, args.limit, args.pages,
                args.date_from, args.date_to,
            )
        print(f"\nНайдено {len(urls)} URL:\n")
        for u in urls:
            print(" ", u)
        return 0

    # Full run
    result = scraper.run(
        urls       = args.urls or None,
        query      = args.query,
        category   = args.category,
        max_docs   = args.limit,
        max_pages  = args.pages,
        date_from  = args.date_from,
        date_to    = args.date_to,
        on_progress = progress,
    )

    # Summary
    print(f"\n{'═'*50}")
    print(f"✅ Сохранено:   {result['saved']}")
    print(f"✗  Ошибок:      {result['failed']}")
    print(f"○  Пропущено:   {result['skipped']}")
    print(f"   Всего URL:   {result['total']}")
    if result.get("error"):
        print(f"\n⚠  {result['error']}")
    print(f"{'═'*50}\n")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"Результаты сохранены в: {args.output}")

    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())