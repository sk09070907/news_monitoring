"""
Groups similar articles together to avoid sending near-duplicate notifications.

Algorithm:
  1. Normalize titles (lowercase, remove brackets/punctuation).
  2. Compute pairwise similarity with difflib.SequenceMatcher.
  3. Greedily assign each article to the first group whose primary title
     is sufficiently similar; otherwise start a new group.
  4. Within a group, multiple sources for the same story are kept so the
     Discord notification can link to all of them.
"""

import difflib
import logging
import re
from dataclasses import dataclass, field

from fetcher import Article

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------


@dataclass
class ArticleGroup:
    articles: list[Article] = field(default_factory=list)
    ai_summary: str = ""

    @property
    def primary(self) -> Article:
        """The article that represents this group (earliest published, or first)."""
        dated = [a for a in self.articles if a.published]
        if dated:
            return min(dated, key=lambda a: a.published)  # type: ignore[arg-type]
        return self.articles[0]

    @property
    def company(self) -> str:
        return self.primary.company

    @property
    def title(self) -> str:
        return self.primary.title


# ------------------------------------------------------------------
# Similarity helpers
# ------------------------------------------------------------------

# Brackets and punctuation that carry no semantic weight
_NOISE_RE = re.compile(r"[【】「」『』〈〉《》\[\]()（）【】｢｣<>|｜・…。、，,!！?？\-－—]")
_WS_RE = re.compile(r"\s+")


def _normalize(title: str) -> str:
    t = title.lower()
    t = _NOISE_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def deduplicate_and_group(articles: list[Article], settings: dict) -> list[ArticleGroup]:
    """
    Group articles that cover the same story.

    Rules:
    - Only articles for the same company can be grouped together.
    - Two articles are "same story" if their title similarity ≥ threshold.
    - Within a group, each source appears at most once (first occurrence wins).
    """
    threshold: float = settings.get("similarity_threshold", 0.65)

    # Sort newest-first so the most recent article becomes the primary
    sorted_articles = sorted(
        articles,
        key=lambda a: a.published.timestamp() if a.published else 0,
        reverse=True,
    )

    groups: list[ArticleGroup] = []

    for article in sorted_articles:
        if not article.title:
            continue

        matched: ArticleGroup | None = None
        for group in groups:
            # Must be the same company
            if group.company != article.company:
                continue
            if _similarity(group.primary.title, article.title) >= threshold:
                matched = group
                break

        if matched is not None:
            # Add only if this source is not already represented
            existing_sources = {a.source for a in matched.articles}
            if article.source not in existing_sources:
                matched.articles.append(article)
                logger.debug(
                    f"グループ追加 [{article.source}] → '{matched.primary.title[:40]}...'"
                )
        else:
            groups.append(ArticleGroup(articles=[article]))

    logger.info(
        f"グループ化完了: {len(articles)} 件 → {len(groups)} グループ"
    )
    return groups
