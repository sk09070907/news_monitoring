"""
Generates AI summaries for article groups using Gemini 2.0 Flash (free tier).
Rate limit on free tier: 15 RPM → enforces a minimum delay between calls.
"""

import logging
import time

from google import genai

from processor import ArticleGroup

logger = logging.getLogger(__name__)

# Gemini free tier: 15 RPM → 1 request per 4 seconds (conservative)
_MIN_INTERVAL_SEC = 4.5


def _build_prompt(group: ArticleGroup, language: str = "日本語") -> str:
    primary = group.primary
    content = (primary.description or primary.title).strip()

    multi_source_note = ""
    if len(group.articles) > 1:
        lines = [f"・{a.source}: {a.title}" for a in group.articles]
        multi_source_note = "\n\n【複数メディアが報道】\n" + "\n".join(lines)

    return (
        f"以下のニュースを{language}で2〜3文に要約してください。\n"
        "重要な数字・企業への影響・背景があれば含めてください。\n"
        "箇条書きは使わず、自然な文章で答えてください。\n"
        f"{multi_source_note}\n\n"
        f"企業名: {primary.company}\n"
        f"タイトル: {primary.title}\n"
        f"内容: {content}\n\n"
        "要約:"
    )


def summarize_articles(
    groups: list[ArticleGroup],
    api_key: str,
    settings: dict,
) -> list[ArticleGroup]:
    """
    Populate `group.ai_summary` for each ArticleGroup using Gemini.
    Respects free-tier RPM limits with a per-call delay.
    """
    cfg = settings.get("summarization", {})
    if not cfg.get("enabled", True):
        logger.info("AI要約は無効化されています (settings.yaml)")
        return groups

    model_name: str = cfg.get("model", "gemini-2.0-flash")
    language: str = cfg.get("language", "日本語")

    client = genai.Client(api_key=api_key)

    success = 0
    skipped = 0
    last_call_time = 0.0
    rate_limited = False

    for group in groups:
        # If rate limit was hit earlier in this run, skip all remaining summaries
        if rate_limited:
            skipped += 1
            continue

        # Rate limiting: ensure minimum interval between API calls
        elapsed = time.time() - last_call_time
        if elapsed < _MIN_INTERVAL_SEC:
            time.sleep(_MIN_INTERVAL_SEC - elapsed)

        try:
            prompt = _build_prompt(group, language)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            group.ai_summary = response.text.strip()
            success += 1
            logger.debug(f"要約完了: {group.title[:50]}")
        except Exception as e:
            # 必ず実際のエラーをログに残す
            logger.error(f"Gemini エラー [{type(e).__name__}]: {e}")
            error_str = str(e).lower()
            # 429 / ResourceExhausted のみレート制限と判定（"rate"は誤検知しやすいため除外）
            is_rate_limit = (
                "429" in str(e)
                or "resource_exhausted" in error_str
                or "quota_exceeded" in error_str
            )
            if is_rate_limit:
                rate_limited = True
                skipped += 1
                logger.warning(
                    "Gemini 無料枠の上限に達しました。"
                    "この実行の残り記事は要約なしで通知します。"
                )
            else:
                # レート制限以外のエラーはこの記事だけスキップして継続
                skipped += 1
        finally:
            last_call_time = time.time()

    if rate_limited:
        logger.warning(f"AI要約: 成功 {success} 件 / レート制限によりスキップ {skipped} 件")
    else:
        logger.info(f"AI要約: 成功 {success} 件")
    return groups
