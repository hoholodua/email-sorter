"""
Smart Digest — ежедневный дайджест удалённых писем с LLM-саммари.

Идея:
  При удалении письма сохраняется его сниппет (первые ~500 символов).
  Раз в день (или по --digest) LLM одной пачкой суммаризирует все удалённые
  письма, группирует по типу и отправляет в Telegram — чтобы пользователь
  ничего важного не пропустил, даже если письмо авто-удалено.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

import requests

from config import Config
from db import get_digest_entries, mark_digest_sent


# Сколько символов сниппета отправлять LLM на одно письмо (чтобы не перегружать)
MAX_SNIPPET_CHARS = 300
# Максимум писем в одном batch-запросе к LLM
MAX_BATCH_ENTRIES = 50
# Лимит символов ответа LLM на дайджест
DIGEST_MAX_TOKENS = 600


def _format_snippet(entry: dict) -> str:
    """Форматирует одну запись для подачи в LLM."""
    sender = entry.get("sender", "")[:60]
    subject = entry.get("subject", "")[:80]
    label = entry.get("label", "") or "разное"
    snippet = entry.get("snippet", "")[:MAX_SNIPPET_CHARS]
    extra = entry.get("extra_info", "")
    extra_str = ""
    if extra:
        try:
            info = json.loads(extra)
            parts = [f"{k}={v}" for k, v in info.items() if v]
            if parts:
                extra_str = " [" + ", ".join(parts) + "]"
        except (json.JSONDecodeError, TypeError):
            pass

    return f"• [{label}] {subject} — {sender}{extra_str}\n  {snippet}"


def _build_batch_prompt(entries: list[dict]) -> str:
    """Строит промпт для LLM: список удалённых писем + запрос на саммари."""
    items = "\n".join(_format_snippet(e) for e in entries)
    return (
        "Ниже список удалённых писем за сегодня. Каждое письмо имеет метку [тип].\n\n"
        f"{items}\n\n"
        "Сгруппируй их по типу и напиши КОРОТКИЙ дайджест (2-3 предложения на группу, "
        "максимум 5 групп). Для каждой группы укажи количество писем.\n"
        "Используй понятный русский язык, как будто рассказываешь коллеге, "
        "какие письма сегодня пришли и были авто-удалены.\n\n"
        "Формат:\n📁 <группа> (<количество> писем)\n<краткое содержание>\n\n"
        "Не пиши 'в этом дайджесте' или 'сегодня были удалены' — просто факты."
    )


def _call_llm(prompt: str, max_tokens: int = DIGEST_MAX_TOKENS) -> str:
    """Вызывает Ollama и возвращает текст ответа."""
    try:
        resp = requests.post(
            f"{Config.OLLAMA_URL}/api/generate",
            json={
                "model": Config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "max_tokens": max_tokens,
                },
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.exceptions.Timeout:
        return "⚠️ Таймаут при генерации дайджеста."
    except Exception as e:
        return f"⚠️ Ошибка LLM: {e}"


def compile_digest(target_date: date | None = None) -> dict[str, Any]:
    """
    Собирает дайджест: выбирает неотправленные записи, группирует,
    генерирует LLM-саммари для каждой группы.

    Returns:
        {
            "date": "2026-07-07",
            "total": 24,
            "by_label": {"реклама": 10, "github": 5, ...},
            "llm_summaries": {"реклама": "текст...", "github": "текст..."},
            "entries": [...],       # полные записи
            "message_ids": [...],   # для отметки digest_sent
        }
    """
    if target_date is None:
        target_date = date.today()

    entries = get_digest_entries(target_date)
    if not entries:
        return {
            "date": target_date.isoformat(),
            "total": 0,
            "by_label": {},
            "llm_summaries": {},
            "entries": [],
            "message_ids": [],
        }

    # Группируем по label
    by_label: dict[str, list[dict]] = {}
    for e in entries:
        label = e.get("label") or "разное"
        by_label.setdefault(label, []).append(e)

    # Генерируем LLM-саммари для каждой группы (если писем > 1, иначе короткое описание)
    llm_summaries: dict[str, str] = {}
    for label, group in by_label.items():
        if len(group) == 1:
            # Одно письмо — кратко из сниппета
            e = group[0]
            subj = e.get("subject", "")[:80]
            sender = e.get("sender", "")[:40]
            llm_summaries[label] = f"{subj} — {sender}"
        else:
            # Несколько — batch LLM
            batches = [group[i:i+MAX_BATCH_ENTRIES] for i in range(0, len(group), MAX_BATCH_ENTRIES)]
            summaries = []
            for batch in batches:
                prompt = _build_batch_prompt(batch)
                result = _call_llm(prompt)
                if result:
                    summaries.append(result)
            llm_summaries[label] = "\n".join(summaries) if summaries else ""

    message_ids = [e["message_id"] for e in entries if e.get("message_id")]

    return {
        "date": target_date.isoformat(),
        "total": len(entries),
        "by_label": {k: len(v) for k, v in by_label.items()},
        "llm_summaries": llm_summaries,
        "entries": entries,
        "message_ids": message_ids,
    }


def format_digest_message(digest: dict[str, Any]) -> str:
    """
    Форматирует дайджест в HTML для Telegram.
    """
    if digest["total"] == 0:
        return (
            f"📋 <b>Дайджест Email Sorter</b>\n"
            f"📅 {digest['date']}\n\n"
            f"✨ Сегодня удалённых писем нет — можно выдохнуть."
        )

    date_formatted = datetime.strptime(digest["date"], "%Y-%m-%d").strftime("%d.%m.%Y")
    text = (
        f"📋 <b>Дайджест удалённых писем</b>\n"
        f"📅 {date_formatted}\n"
        f"Всего: <b>{digest['total']}</b>\n\n"
    )

    # Сортировка групп: от самой большой к маленькой
    sorted_labels = sorted(
        digest["by_label"].items(),
        key=lambda x: x[1],
        reverse=True,
    )

    emoji_map = {
        "реклама": "📢", "promotions": "📢",
        "рассылка": "📬", "newsletter": "📬",
        "github": "🐙", "git": "🐙",
        "уведомления": "🔔", "notifications": "🔔",
        "соцсети": "📱", "social": "📱", "linkedin": "💼",
        "банк": "🏦", "finance": "💰", "bank": "🏦",
        "доставка": "📦", "delivery": "📦",
        "работа": "💼", "work": "💼",
        "разное": "📎",
    }

    for label, count in sorted_labels:
        emoji = "📎"
        for key, e in emoji_map.items():
            if key in label.lower():
                emoji = e
                break

        label_display = label.capitalize()
        text += f"{emoji} <b>{label_display}</b> — {count} писем\n"

        summary = digest["llm_summaries"].get(label, "")
        if summary:
            # Обрезаем длинные саммари до 400 символов
            summary_short = summary[:400]
            text += f"  {summary_short}\n"

        text += "\n"

    text += "💡 Письма в корзине Gmail — можно восстановить в течение 30 дней."
    return text


def send_digest(
    telegram,
    target_date: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Полный цикл: собрать дайджест → отправить в Telegram → отметить как отправленные.

    Args:
        telegram: TelegramNotifier instance.
        target_date: Дата для дайджеста (по умолчанию сегодня).
        dry_run: Не отмечать как отправленные.

    Returns:
        Результат compile_digest().
    """
    if target_date is None:
        target_date = date.today()

    print(f"\n📋 Сбор дайджеста за {target_date.isoformat()}...")
    digest = compile_digest(target_date)
    print(f"   Найдено записей: {digest['total']}")

    if digest["total"] == 0:
        msg = format_digest_message(digest)
        if telegram and telegram.enabled:
            telegram.send(msg)
        print("   ✨ Нет удалённых писем за сегодня.")
        return digest

    msg = format_digest_message(digest)
    print(f"   Длина сообщения: {len(msg)} символов")

    if telegram and telegram.enabled:
        telegram.send(msg)
        print("   ✅ Дайджест отправлен в Telegram")

    if not dry_run and digest["message_ids"]:
        mark_digest_sent(digest["message_ids"])
        print(f"   ✅ Отмечено {len(digest['message_ids'])} записей как отправленные")

    return digest
