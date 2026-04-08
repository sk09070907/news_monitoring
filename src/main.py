#!/usr/bin/env python3
"""
News Monitoring Tool - Entry Point

Usage:
  python src/main.py

Environment variables required:
  DISCORD_WEBHOOK_URL  - Discord incoming webhook URL
  ANTHROPIC_API_KEY    - Anthropic API key (optional; skips AI summary if absent)
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow importing sibling modules without installing the package
sys.path.insert(0, str(Path(__file__).parent))

import yaml
from dotenv import load_dotenv

from fetcher import fetch_all_articles
from logger_setup import setup_logging
from notifier import send_discord_notifications
from processor import deduplicate_and_group
from state_manager import StateManager
from summarizer import summarize_articles

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"


# ------------------------------------------------------------------
# Config loading
# ------------------------------------------------------------------


def _load_config() -> tuple[dict, dict]:
    with open(CONFIG_DIR / "companies.yaml", "r", encoding="utf-8") as f:
        companies_cfg: dict = yaml.safe_load(f)
    with open(CONFIG_DIR / "settings.yaml", "r", encoding="utf-8") as f:
        settings: dict = yaml.safe_load(f)
    return companies_cfg, settings


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------


def main() -> None:
    load_dotenv()
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("ニュースモニタリング 開始")

    # ---- Load configuration ----------------------------------------
    try:
        companies_cfg, settings = _load_config()
    except FileNotFoundError as e:
        logger.error(f"設定ファイルが見つかりません: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"設定ファイル読み込みエラー: {e}")
        sys.exit(1)

    companies: list[dict] = companies_cfg.get("companies") or []
    if not companies:
        logger.warning(
            "モニタリング対象企業が設定されていません。"
            "config/companies.yaml に企業を追加してください。"
        )
        return

    logger.info(f"モニタリング対象: {[c['name'] for c in companies]}")

    # ---- Fetch articles --------------------------------------------
    all_articles = fetch_all_articles(companies, settings)
    logger.info(f"RSS 取得件数: {len(all_articles)}")

    # ---- Date filter: drop articles older than max_article_age_hours ----
    max_age_hours: int = settings.get("max_article_age_hours", 48)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    before = len(all_articles)
    all_articles = [
        a for a in all_articles
        if a.published is None or a.published >= cutoff
    ]
    dropped = before - len(all_articles)
    if dropped:
        logger.info(f"日付フィルタ: {dropped} 件を除外 ({max_age_hours}時間以上前の記事)")

    # ---- Filter already-seen articles ------------------------------
    state = StateManager()
    new_articles = state.filter_new(all_articles)

    if not new_articles:
        logger.info("新規記事なし。終了します。")
        # Still run cleanup and save to update last_updated timestamp
        cleanup_days: int = settings.get("state", {}).get("cleanup_days", 7)
        state.cleanup_old_entries(cleanup_days)
        state.save()
        logger.info("=" * 60)
        return

    logger.info(f"新規記事: {len(new_articles)} 件")

    # ---- Group similar articles ------------------------------------
    groups = deduplicate_and_group(new_articles, settings)

    # ---- AI summarization ------------------------------------------
    api_key = os.environ.get("GROQ_API_KEY", "")
    if api_key and settings.get("summarization", {}).get("enabled", True):
        logger.info("AI 要約を生成中...")
        groups = summarize_articles(groups, api_key, settings)
    else:
        if not api_key:
            logger.warning("GROQ_API_KEY 未設定 → AI 要約をスキップ")

    # ---- Discord notifications -------------------------------------
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if webhook_url:
        send_discord_notifications(groups, webhook_url, settings)
    else:
        logger.error("DISCORD_WEBHOOK_URL が未設定です")

    # ---- Persist state --------------------------------------------
    state.mark_seen(new_articles)
    cleanup_days = settings.get("state", {}).get("cleanup_days", 7)
    state.cleanup_old_entries(cleanup_days)
    state.save()

    logger.info(f"完了: {len(groups)} グループを通知")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
