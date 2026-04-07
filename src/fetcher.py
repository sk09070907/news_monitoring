"""
Fetches news articles from Google News RSS feeds.
All sources (Reuters, Bloomberg, PR TIMES, general) use Google News RSS
for maximum stability and zero cost.
"""

import html as html_mod
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import feedparser
import requests

logger = logging.getLogger(__name__)

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; NewsMonitor/1.0; "
        "+https://github.com/your-username/monitoring_news_claude)"
    )
}
_REQUEST_TIMEOUT = 20  # seconds


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


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def _extract_keywords(company_cfg: dict) -> list[str]:
    """
    Build a deduplicated keyword list from all name fields.

    Field priority (all non-empty values become search keywords):
      official, english, short, ticker, code, extra[]
    If none are set, falls back to `name`.
    """
    name: str = company_cfg.get("name", "")
    candidates = [
        company_cfg.get("official", ""),
        company_cfg.get("english", ""),
        company_cfg.get("short", ""),
        company_cfg.get("ticker", ""),
        company_cfg.get("code", ""),
        # legacy flat keywords list (backwards compatible)
        *company_cfg.get("keywords", []),
        # new extra list
        *company_cfg.get("extra", []),
    ]
    # Keep insertion order, drop empty strings and duplicates
    seen: set[str] = set()
    result: list[str] = []
    for kw in candidates:
        kw = kw.strip()
        if kw and kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result or [name]


def fetch_all_articles(companies: list[dict], settings: dict) -> list[Article]:
    """
    Fetch articles for all companies from all configured sources.
    Returns a deduplicated list of Article objects.
    """
    sources = [s for s in settings.get("news_sources", []) if s.get("enabled", True)]
    max_per_source = settings.get("max_articles_per_source", 30)

    all_articles: list[Article] = []

    for company_cfg in companies:
        company_name: str = company_cfg["name"]
        keywords: list[str] = _extract_keywords(company_cfg)

        for keyword in keywords:
            for source in sources:
                source_type = source.get("type", "google_news")

                if source_type == "google_news":
                    url = _build_google_news_url(
                        keyword=keyword,
                        site_filter=source.get("site_filter", ""),
                        language=source.get("language", "ja"),
                        country=source.get("country", "JP"),
                    )
                    articles = _fetch_rss(url, company_name, source["name"], max_per_source)
                    all_articles.extend(articles)
                else:
                    logger.warning(f"未知のソースタイプ: {source_type}")

                time.sleep(0.8)  # Polite delay between requests

            time.sleep(0.5)  # Delay between keywords

    # Deduplicate by exact URL
    seen_urls: set[str] = set()
    unique: list[Article] = []
    for a in all_articles:
        if a.url not in seen_urls:
            seen_urls.add(a.url)
            unique.append(a)

    logger.info(f"RSS 取得完了: 全{len(all_articles)}件 → URL重複除去後 {len(unique)}件")
    return unique
