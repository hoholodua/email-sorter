"""
Дополнительные анализаторы писем (probes), запускаемые после основной классификации.

Включает:
- Извлечение задач (todos) из писем
- Детекция подписок (Subscription Radar)
- Трекинг расходов (Spend Tracking)

Каждый probe — отдельная функция, которая принимает email_text + extra_info
и сохраняет результат в БД (если что-то найдено).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

import requests

from config import Config


PROBE_MODEL = Config.OLLAMA_MODEL

# ─── Вспомогательные функции ──────────────────────────────

def _llm_ask(prompt: str, max_tokens: int = 200) -> str:
    """Короткий запрос к LLM без сохранения в историю."""
    try:
        resp = requests.post(
            f"{Config.OLLAMA_URL}/api/generate",
            json={
                "model": PROBE_MODEL,
                "prompt": prompt,
                "stream": False,
                "temperature": 0.1,
                "max_tokens": max_tokens,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception:
        return ""


# ─── Извлечение задач ─────────────────────────────────────

TASK_PROMPT = """Extract any task/todo from this email. A task is something the user needs to DO (reply, review, submit, buy, call, etc.).

If there is a task, respond with JSON:
{{"found": true, "task": "short task description", "due": "due date or empty", "priority": "high/medium/low"}}

If there is no task, respond:
{{"found": false}}

Email:
{text}"""


def extract_tasks(email_text: str, subject: str, sender: str,
                  decision: str) -> list[dict[str, Any]]:
    """
    Извлекает задачи из письма.

    Returns:
        Список задач: [{"task": "...", "due": "...", "priority": "..."}]
    """
    results: list[dict[str, Any]] = []

    # Не извлекаем задачи из спама
    if decision in ("delete",):
        return results

    text = f"Subject: {subject}\nFrom: {sender}\nBody: {email_text[:2000]}"
    prompt = TASK_PROMPT.format(text=text)
    raw = _llm_ask(prompt)

    try:
        data = json.loads(raw)
        if data.get("found"):
            results.append({
                "task": data.get("task", ""),
                "due": data.get("due", ""),
                "priority": data.get("priority", "medium"),
            })
    except (json.JSONDecodeError, TypeError):
        # Пробуем извлечь вручную
        m = re.search(r'"task"\s*:\s*"([^"]+)"', raw)
        if m:
            results.append({
                "task": m.group(1),
                "due": "",
                "priority": "medium",
            })

    return results


# ─── Детекция подписок ────────────────────────────────────

SUBSCRIPTION_PROMPT = """Is this email about a paid subscription or recurring service?
Look for: subscription confirmation, renewal, payment, trial ending, plan upgrade/downgrade, invoice.

Respond with JSON:
{{"is_subscription": true, "service": "service name", "amount": "amount or empty", "period": "monthly/yearly/weekly", "next_billing": "date or empty"}}
or
{{"is_subscription": false}}

Email:
{text}"""


def detect_subscription(email_text: str, subject: str, sender: str,
                        extra_info: dict[str, Any]) -> dict[str, Any] | None:
    """
    Определяет, является ли письмо информацией о подписке.

    Returns:
        {"service": "...", "amount": "...", "period": "...", "next_billing": "..."} или None
    """
    # Быстрая проверка по ключевым словам (без LLM)
    text_lower = f"{subject} {email_text[:500]}".lower()
    sub_keywords = [
        "subscription", "подписк", "renewal", "продлен",
        "trial", "пробн", "billing", "invoice", "receipt",
        "premium", "pro plan", "monthly fee",
    ]
    if not any(kw in text_lower for kw in sub_keywords):
        return None

    # Вызываем LLM для точного распознавания
    text = f"Subject: {subject}\nFrom: {sender}\nBody: {email_text[:1500]}"
    prompt = SUBSCRIPTION_PROMPT.format(text=text)
    raw = _llm_ask(prompt, max_tokens=150)

    try:
        data = json.loads(raw)
        if data.get("is_subscription"):
            return {
                "service": data.get("service", sender),
                "amount": data.get("amount", ""),
                "period": data.get("period", "monthly"),
                "next_billing": data.get("next_billing", ""),
            }
    except (json.JSONDecodeError, TypeError):
        pass

    return None


# ─── Трекинг расходов ─────────────────────────────────────

SPEND_PROMPT = """Extract financial transaction details from this email.
Only respond if this is a purchase, payment, transfer, or expense notification.

Respond with JSON:
{{"is_expense": true, "amount": "1234.56", "currency": "RUB/USD/EUR", "merchant": "store name", "category": "food/transport/shopping/bills/entertainment/other"}}
or
{{"is_expense": false}}

Email:
{text}"""


def extract_expense(email_text: str, subject: str, sender: str,
                    extra_info: dict[str, Any]) -> dict[str, Any] | None:
    """
    Извлекает информацию о расходе из письма.

    Returns:
        {"amount": "...", "currency": "...", "merchant": "...", "category": "..."} или None
    """
    # Если extra_info уже содержит сумму — используем её без LLM
    if extra_info:
        amount = extra_info.get("amount") or extra_info.get("order_number")
        if amount and extra_info.get("merchant") or extra_info.get("store"):
            return {
                "amount": str(extra_info.get("amount", "")),
                "currency": extra_info.get("currency", "RUB"),
                "merchant": str(extra_info.get("merchant") or extra_info.get("store", "")),
                "category": _guess_category(extra_info.get("merchant", "") or
                                            extra_info.get("store", "")),
            }

    text = f"Subject: {subject}\nFrom: {sender}\nBody: {email_text[:1500]}"
    prompt = SPEND_PROMPT.format(text=text)
    raw = _llm_ask(prompt, max_tokens=150)

    try:
        data = json.loads(raw)
        if data.get("is_expense"):
            return {
                "amount": str(data.get("amount", "")),
                "currency": data.get("currency", "RUB"),
                "merchant": data.get("merchant", ""),
                "category": data.get("category", "other"),
            }
    except (json.JSONDecodeError, TypeError):
        pass

    return None


def _guess_category(merchant: str) -> str:
    """Определяет категорию трат по имени магазина."""
    m = merchant.lower()
    if any(kw in m for kw in ["авито", "ozon", "wb", "wildberries", "amazon", "ebay", "etsy"]):
        return "shopping"
    if any(kw in m for kw in ["macdonald", "kfc", "burger", "еда", "delivery", "доставк"]):
        return "food"
    if any(kw in m for kw in ["билайн", "мтс", "tele2", "megafon", "тпс"]):
        return "bills"
    if any(kw in m for kw in ["netflix", "spotify", "youtube", "apple music"]):
        return "entertainment"
    if any(kw in m for kw in ["uber", "yandex taxi", "яндекс такси", "citymobil"]):
        return "transport"
    return "other"


# ─── Единый запуск всех probes ────────────────────────────

def run_all_probes(email_text: str, subject: str, sender: str,
                    decision: str, extra_info: dict[str, Any]) -> dict[str, Any]:
    """
    Запускает все анализаторы после классификации.

    Returns:
        {
            "tasks": [...],
            "subscription": {...} | None,
            "expense": {...} | None,
        }
    """
    results: dict[str, Any] = {
        "tasks": [],
        "subscription": None,
        "expense": None,
    }

    # Tasks — только для keep/review
    if decision in ("keep", "review"):
        results["tasks"] = extract_tasks(email_text, subject, sender, decision)

    # Subscription — пробуем для всех, кроме delete
    if decision != "delete":
        results["subscription"] = detect_subscription(
            email_text, subject, sender, extra_info
        )

    # Expense — для receipt/notification/bank
    results["expense"] = extract_expense(
        email_text, subject, sender, extra_info
    )

    return results
