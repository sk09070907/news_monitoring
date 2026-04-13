"""
Manages persistent state: which article URLs have already been notified.
State is stored in data/seen_articles.json and committed back to the repo
by the GitHub Actions workflow after each run.
"""

import difflib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fetcher import Article

_NOISE_RE = re.compile(r"[【】「」『』〈〉《》\[\]()（）｢｣<>|｜・…。、，,!！?？\-－—]")
_WS_RE = re.compile(r"\s+")


def _normalize(title: str) -> str:
    t = title.lower()
    t = _NOISE_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def _title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
STATE_FILE = DATA_DIR / "seen_articles.json"


class StateManager:
    def __init__(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        self._state = self._load()

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"状態ファイル読み込み: {len(data.get('articles', {}))} 件")
                return data
            except Exception as e:
                logger.error(f"状態ファイル読み込みエラー: {e}")
        return {"articles": {}}

    def save(self) -> None:
        try:
            self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            logger.info(f"状態ファイル保存: {len(self._state['articles'])} 件")
        except Exception as e:
            logger.error(f"状態ファイル保存エラー: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def filter_new(self, articles: list["Article"]) -> list["Article"]:
        """Return only articles whose URL has not been seen before."""
        seen = set(self._state.get("articles", {}).keys())
        new = [a for a in articles if a.url and a.url not in seen]
        logger.info(
            f"新規フィルタリング: 全{len(articles)}件 → 新規{len(new)}件 "
            f"(既読 {len(articles) - len(new)} 件スキップ)"
        )
        return new

    def mark_seen(self, articles: list["Article"]) -> None:
        """Record articles as seen."""
        now = datetime.now(timezone.utc).isoformat()
        bucket = self._state.setdefault("articles", {})
        for a in articles:
            if a.url:
                bucket[a.url] = {
                    "title": a.title,
                    "company": a.company,
                    "source": a.source,
                    "seen_at": now,
                }

    def filter_seen_by_title(
        self,
        groups: list,
        threshold: float = 0.75,
        hours: int = 24,
    ) -> list:
        """
        グループのタイトルが、過去N時間以内に通知済みの記事と
        同一企業・類似タイトル（threshold以上）であれば除外する。
        URLが異なる同じ話題の重複通知を防ぐ。
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        bucket = self._state.get("articles", {})

        # 企業ごとに最近のタイトル一覧を作成
        recent_by_company: dict[str, list[str]] = {}
        for info in bucket.values():
            try:
                seen_at = datetime.fromisoformat(info["seen_at"])
                if seen_at.tzinfo is None:
                    seen_at = seen_at.replace(tzinfo=timezone.utc)
                if seen_at < cutoff:
                    continue
                company = info.get("company", "")
                title = info.get("title", "")
                if company and title:
                    recent_by_company.setdefault(company, []).append(title)
            except Exception:
                pass

        filtered = []
        skipped = 0
        for group in groups:
            company = group.company
            title = group.title
            recent_titles = recent_by_company.get(company, [])
            is_dup = any(
                _title_similarity(title, seen_title) >= threshold
                for seen_title in recent_titles
            )
            if is_dup:
                logger.info(f"クロスラン重複除外: [{company}] {title[:60]}")
                skipped += 1
            else:
                filtered.append(group)

        if skipped:
            logger.info(f"クロスラン重複フィルタ: {skipped} 件を除外")
        return filtered

    def cleanup_old_entries(self, days: int = 7) -> None:
        """Delete entries older than `days` days to keep the file small."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        bucket = self._state.get("articles", {})
        to_remove = []

        for url, info in bucket.items():
            try:
                seen_at = datetime.fromisoformat(info["seen_at"])
                if seen_at.tzinfo is None:
                    seen_at = seen_at.replace(tzinfo=timezone.utc)
                if seen_at < cutoff:
                    to_remove.append(url)
            except Exception:
                pass

        for url in to_remove:
            del bucket[url]

        if to_remove:
            logger.info(f"古いエントリを削除: {len(to_remove)} 件 ({days}日以上前)")
