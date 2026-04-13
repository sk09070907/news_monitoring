"""
Sends Discord notifications via Webhook using rich Embeds.

Layout:
  - Single-source article  → embed with title linked to the article URL
  - Multi-source group     → embed listing all source links in a field
  - Articles are batched (≤10 embeds per Discord message)
  - Companies get distinct colors that persist across a run
"""

import logging
import time
from datetime import datetime, timezone

import requests

from processor import ArticleGroup

logger = logging.getLogger(__name__)

# 重要記事の色
_IMPORTANT_COLOR = 0xFF0000  # 赤

# Palette of Discord embed colors cycled per company
_PALETTE = [
    0x5865F2,  # Blurple
    0x57F287,  # Green
    0xFEE75C,  # Yellow
    0xEB459E,  # Fuchsia
    0x9B59B6,  # Purple
    0xE67E22,  # Orange
    0x1ABC9C,  # Teal
    0x3498DB,  # Blue
]


# ------------------------------------------------------------------
# Embed builder
# ------------------------------------------------------------------


def _fmt_published(article) -> str:
    if article.published:
        return article.published.strftime("%Y-%m-%d %H:%M UTC")
    return "不明"


def _build_embed(group: ArticleGroup, color: int) -> dict:
    primary = group.primary

    # 重要記事は色を赤に上書き
    if group.is_important:
        color = _IMPORTANT_COLOR

    # Description: AI summary → description → title (fallback)
    description = (group.ai_summary or primary.description or primary.title).strip()
    if len(description) > 350:
        description = description[:347] + "…"

    # 重要ラベルとAIスコアをフッターに追加
    footer_parts = [f"🏢 {group.company}"]
    if group.is_important:
        footer_parts.append("⚠️ 重要")
    if group.importance_score > 0:
        footer_parts.append(f"重要度: {'★' * group.importance_score}{'☆' * (5 - group.importance_score)}")
    footer_text = "　".join(footer_parts)

    embed: dict = {
        "color": color,
        "description": description,
        "footer": {"text": footer_text},
    }

    if primary.published:
        embed["timestamp"] = primary.published.isoformat()

    if len(group.articles) == 1:
        # ---- Single source ----------------------------------------
        title = primary.title[:256]
        if group.is_important:
            title = ("⚠️ " + primary.title)[:256]
        embed["title"] = title
        embed["url"] = primary.url
        embed["fields"] = [
            {"name": "ソース", "value": primary.source, "inline": True},
            {"name": "公開日時", "value": _fmt_published(primary), "inline": True},
        ]
    else:
        # ---- Multiple sources (same story) ------------------------
        base_title = f"[{len(group.articles)}メディア] {primary.title[:220]}"
        if group.is_important:
            base_title = ("⚠️ " + base_title)[:256]
        embed["title"] = base_title
        source_lines = [
            f"・[{a.source}]({a.url})" for a in group.articles[:8]
        ]
        embed["fields"] = [
            {
                "name": f"ソース ({len(group.articles)} 件)",
                "value": "\n".join(source_lines),
                "inline": False,
            },
            {"name": "最初の公開", "value": _fmt_published(primary), "inline": True},
        ]

    return embed


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def send_discord_notifications(
    groups: list[ArticleGroup],
    webhook_url: str,
    settings: dict,
) -> None:
    """Send all article groups to Discord, batched to stay within API limits."""
    if not groups:
        return

    max_per_msg: int = settings.get("discord", {}).get("max_embeds_per_message", 10)
    delay: float = settings.get("discord", {}).get("delay_between_messages", 1.5)

    # Assign colors per company
    company_color: dict[str, int] = {}
    for i, group in enumerate(groups):
        if group.company not in company_color:
            company_color[group.company] = _PALETTE[len(company_color) % len(_PALETTE)]

    embeds = [_build_embed(g, company_color[g.company]) for g in groups]

    total_sent = 0
    for batch_start in range(0, len(embeds), max_per_msg):
        batch = embeds[batch_start : batch_start + max_per_msg]
        payload = {
            "username": "News Monitor",
            "embeds": batch,
        }

        try:
            resp = requests.post(webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
            total_sent += len(batch)
            logger.info(f"Discord 送信: {len(batch)} 件 (累計 {total_sent}/{len(embeds)})")
        except requests.exceptions.HTTPError as e:
            logger.error(f"Discord HTTP エラー: {e} / レスポンス: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Discord 送信エラー: {e}")

        # Rate-limit guard between batches
        if batch_start + max_per_msg < len(embeds):
            time.sleep(delay)

    logger.info(f"Discord 通知完了: {total_sent} 件送信")
