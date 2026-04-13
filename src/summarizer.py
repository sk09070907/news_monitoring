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
        f"以下のニュース記事を分析してください。\n\n"
        f"タイトル: {primary.title}\n"
        f"記事内容: {content}\n"
        f"{multi_source_note}\n\n"
        "【出力フォーマット（必ずこの形式で）】\n"
        "SCORE: [1-5の整数のみ]\n"
        "SUMMARY: [要約文]\n\n"
        "【スコア基準】\n"
        "5: 買収・合併・倒産・重大事故・不祥事・大幅業績修正など経営に直結する重大ニュース\n"
        "4: 新規事業・大型契約・提携・訴訟・行政処分など注目度の高いニュース\n"
        "3: 通常の事業活動・製品発表・人事以外の発表\n"
        "2: 軽微な発表・業界全般の話題\n"
        "1: 株価情報・定例報告・PR記事など重要度が低いもの\n\n"
        "【要約の注意事項】\n"
        f"・{language}で2〜3文\n"
        "・記事に書かれている出来事・事実・数字のみを要約すること\n"
        "・企業の一般的な説明や背景知識は一切不要\n"
        "・箇条書きは使わず自然な文章で"
    )


def _parse_response(text: str) -> tuple[str, int]:
    """AIレスポンスからSCOREとSUMMARYを抽出する。"""
    score = 0
    summary = text.strip()

    lines = text.strip().splitlines()
    summary_lines = []
    for line in lines:
        if line.startswith("SCORE:"):
            try:
                score = int(line.replace("SCORE:", "").strip())
                score = max(1, min(5, score))  # 1-5にクランプ
            except ValueError:
                pass
        elif line.startswith("SUMMARY:"):
            summary_lines.append(line.replace("SUMMARY:", "").strip())
        elif summary_lines:
            summary_lines.append(line)

    if summary_lines:
        summary = " ".join(summary_lines).strip()

    return summary, score


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
            raw = response.choices[0].message.content.strip()
            summary, score = _parse_response(raw)
            group.ai_summary = summary
            group.importance_score = score
            # スコア4以上は重要フラグを立てる（キーワード判定と合算）
            if score >= 4:
                group.is_important = True
            success += 1
            logger.debug(f"要約完了 [スコア{score}]: {group.title[:50]}")

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
