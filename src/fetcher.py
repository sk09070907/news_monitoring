"""
Fetches news articles from Google News RSS feeds.
All sources (Reuters, Bloomberg, PR TIMES, general) use Google News RSS
for maximum stability and zero cost.
Requests are parallelized with ThreadPoolExecutor to stay within GitHub Actions timeout.
"""

import html as html_mod
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import feedparser
import requests

logger = logging.getLogger(__name__)

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; NewsMonitor/1.0; "
        "+https://github.com/sk09070907/news_monitoring)"
    )
}
_REQUEST_TIMEOUT = 20  # seconds
_MAX_WORKERS = 5       # parallel RSS fetches (too high causes SSL errors from Google)


# ------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------


@dataclass
class Article:
    title: str
    url: str
    source: str
    company: str
    published: Optional[datetime] = None
    description: Optional[str] = None
    ai_summary: Optional[str] = None


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_published(entry) -> Optional[datetime]:
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _fetch_rss(url: str, company: str, source_name: str, max_items: int) -> list[Article]:
    """Download and parse a single RSS feed URL."""
    articles: list[Article] = []
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT, headers=_HTTP_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)

        if feed.bozo and not feed.entries:
            logger.warning(f"RSS パースエラー ({source_name}): {feed.bozo_exception}")
            return articles

        for entry in feed.entries[:max_items]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue

            desc = None
            if hasattr(entry, "summary") and entry.summary:
                desc = _strip_html(entry.summary)[:400] or None

            articles.append(
                Article(
                    title=title,
                    url=link,
                    source=source_name,
                    company=company,
                    published=_parse_published(entry),
                    description=desc,
                )
            )

        logger.debug(f"{source_name} / {company}: {len(articles)} 件取得")
    except requests.exceptions.Timeout:
        logger.warning(f"タイムアウト: {source_name} ({company})")
    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP エラー ({source_name} / {company}): {e}")
    except Exception as e:
        logger.error(f"予期しないエラー ({source_name} / {company}): {e}")

    return articles


def _build_google_news_url(keyword: str, site_filter: str, language: str, country: str) -> str:
    query = f'"{keyword}"'
    if site_filter:
        query += f" site:{site_filter}"
    encoded = quote(query)
    return (
        f"https://news.google.com/rss/search"
        f"?q={encoded}&hl={language}&gl={country}&ceid={country}:{language}"
    )


def _extract_keywords(company_cfg: dict) -> list[str]:
    """
    Build a deduplicated keyword list from all name fields.
    """
    name: str = company_cfg.get("name", "")
    candidates = [
        name,
        company_cfg.get("official", ""),
        company_cfg.get("english", ""),
        company_cfg.get("short", ""),
        company_cfg.get("ticker", ""),
        company_cfg.get("code", ""),
        *company_cfg.get("keywords", []),
        *company_cfg.get("extra", []),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for kw in candidates:
        kw = kw.strip()
        if kw and kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result or [name]


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def fetch_all_articles(companies: list[dict], settings: dict) -> list[Article]:
    """
    Fetch articles for all companies from all configured sources.
    Uses ThreadPoolExecutor for parallel RSS fetching.
    """
    sources = [s for s in settings.get("news_sources", []) if s.get("enabled", True)]
    max_per_source = settings.get("max_articles_per_source", 30)

    # Build all (url, company_name, source_name) tasks upfront
    # Key optimization: use only ONE search keyword per source per company
    #   - Japanese sources → search by `name` (日本語名)
    #   - English sources  → search by `english` (英語名), fallback to `name`
    # All other keywords (short, code, extra...) are used only in the relevance filter.
    # This reduces requests from ~(keywords × sources) to (1 × sources) per company.
    tasks: list[tuple[str, str, str]] = []
    keyword_map: dict[str, list[str]] = {}

    for company_cfg in companies:
        company_name: str = company_cfg["name"]
        keyword_map[company_name] = _extract_keywords(company_cfg)

        ja_keyword = company_cfg.get("name", "")
        en_keyword = company_cfg.get("english", "") or company_cfg.get("name", "")

        for source in sources:
            if source.get("type", "google_news") != "google_news":
                continue
            lang = source.get("language", "ja")
            keyword = en_keyword if lang == "en" else ja_keyword
            url = _build_google_news_url(
                keyword=keyword,
                site_filter=source.get("site_filter", ""),
                language=lang,
                country=source.get("country", "JP"),
            )
            tasks.append((url, company_name, source["name"]))

    logger.info(f"RSS フェッチ開始: {len(tasks)} リクエスト / 並列数 {_MAX_WORKERS}")

    all_articles: list[Article] = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {
            executor.submit(_fetch_rss, url, company, source_name, max_per_source): (company, source_name)
            for url, company, source_name in tasks
        }
        for future in as_completed(futures):
            try:
                all_articles.extend(future.result())
            except Exception as e:
                company, source_name = futures[future]
                logger.error(f"フェッチ失敗 ({source_name} / {company}): {e}")

    # Deduplicate by exact URL
    seen_urls: set[str] = set()
    unique: list[Article] = []
    for a in all_articles:
        if a.url not in seen_urls:
            seen_urls.add(a.url)
            unique.append(a)

    # Relevance filter: title or description must contain at least one keyword
    relevant: list[Article] = []
    noise_count = 0
    for a in unique:
        keywords_for_company = keyword_map.get(a.company, [])
        text = (a.title + " " + (a.description or "")).lower()
        if any(kw.lower() in text for kw in keywords_for_company):
            relevant.append(a)
        else:
            noise_count += 1
            logger.debug(f"ノイズ除外: [{a.company}] {a.title[:60]}")

    if noise_count:
        logger.info(f"関連性フィルタ: {noise_count} 件のノイズを除外")

    logger.info(f"RSS 取得完了: 全{len(all_articles)}件 → 重複・ノイズ除外後 {len(relevant)}件")
    return relevant
