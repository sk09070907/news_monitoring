"""
Generates AI summaries for article groups using Groq (free tier).
Free tier: 30 RPM / 14,400 RPD — much more generous than Gemini.
"""

import logging
import time

from groq import Groq, RateLimitError

from processor import ArticleGroup

logger = logging.getLogger(__name__)

# Groq free tier: 30 RPM → 1 request per 2 seconds (conservative)
_MIN_INTERVAL_SEC = 2.5


def _build_prompt(group: ArticleGroup, language: str = "日本語") -> str:
    primary = group.primary
    content = (primary.description or primary.title).strip()

    multi_source_note = ""
    if len(group.articles) > 1:
        lines = [f"・{a.source}: {a.title}" for a in group.articles]
        multi_source_note = "\n\n【複数メディアが報道】\n" + "\n".join(lines)

    return (
        f"以下のニュース記事の内容を{language}で2〜3文に要約してください。\n"
        "【重要】記事に書かれている出来事・事実・数字のみを要約すること。"
        "企業の一般的な説明や背景知識は一切不要。\n"
        "箇条書きは使わず、自然な文章で答えてください。\n"
        f"{multi_source_note}\n\n"
        f"タイトル: {primary.title}\n"
        f"記事内容: {content}\n\n"
        "要約:"
    )


def summarize_articles(
    groups: list[ArticleGroup],
    api_key: str,
    settings: dict,
) -> list[ArticleGroup]:
    """
    Populate `group.ai_summary` for each ArticleGroup using Groq.
    Stops summarizing (but still notifies) if rate limit is hit.
    """
    cfg = settings.get("summarization", {})
    if not cfg.get("enabled", True):
        logger.info("AI要約は無効化されています (settings.yaml)")
        return groups

    model_name: str = cfg.get("model", "llama-3.3-70b-versatile")
    max_tokens: int = cfg.get("max_tokens", 300)
    language: str = cfg.get("language", "日本語")

    client = Groq(api_key=api_key)

    success = 0
    skipped = 0
    rate_limited = False
    last_call_time = 0.0

    for group in groups:
        if rate_limited:
            skipped += 1
            continue

        elapsed = time.time() - last_call_time
        if elapsed < _MIN_INTERVAL_SEC:
            time.sleep(_MIN_INTERVAL_SEC - elapsed)

        try:
            prompt = _build_prompt(group, language)
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
            group.ai_summary = response.choices[0].message.content.strip()
            success += 1
            logger.debug(f"要約完了: {group.title[:50]}")

        except RateLimitError as e:
            rate_limited = True
            skipped += 1
            logger.warning(f"Groq レート制限に達しました。残り記事は要約なしで通知します。({e})")

        except Exception as e:
            logger.error(f"Groq エラー [{type(e).__name__}]: {e}")
            skipped += 1

        finally:
            last_call_time = time.time()

    if rate_limited:
        logger.warning(f"AI要約: 成功 {success} 件 / レート制限によりスキップ {skipped} 件")
    else:
        logger.info(f"AI要約: 成功 {success} 件 / スキップ {skipped} 件")

    return groups
