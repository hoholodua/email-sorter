"""
Сканер корзины (Trash) для авто-коррекции решений.

Сравнивает содержимое Gmail Trash с БД решений:
- Если письмо было помечено как "delete" в БД, но его НЕТ в корзине
  (пользователь восстановил) → записываем коррекцию
- Если письмо было помечено как "keep" в БД, но оно В корзине
  (пользователь вручную удалил) → тоже записываем коррекцию

На основе коррекций обновляются правила отправителей (sender_rules).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from gmail_service import get_service, _get_email_text
from db import record_correction, get_decision, set_sender_rule


def _get_header(msg: dict, name: str) -> str:
    """Достаёт заголовок из письма Gmail API."""
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    return headers.get(name.lower(), "")


def scan_trash(max_results: int = 200) -> dict[str, Any]:
    """
    Сканирует корзину Gmail и находит расхождения с БД.

    Args:
        max_results: Максимум писем для проверки.

    Returns:
        {"checked": int, "corrections": int, "errors": int, "details": [...]}
    """
    service = get_service()
    if not service:
        return {"checked": 0, "corrections": 0, "errors": 0, "details": []}

    print(f"\n🔍 Сканирование корзины (Trash)...")
    stats: dict[str, Any] = {"checked": 0, "corrections": 0, "errors": 0, "details": []}

    try:
        # Получаем список писем в корзине
        results = (
            service.users().messages()
            .list(userId="me", q="in:trash", maxResults=max_results)
            .execute()
        )
        trash_ids: set[str] = {m["id"] for m in results.get("messages", [])}

        if not trash_ids:
            print("  📭 Корзина пуста.")
            return stats

        print(f"  📨 Писем в корзине: {len(trash_ids)}")

        # Получаем из БД недавние решения "delete" (последние 500)
        from db import get_history
        recent = get_history(limit=500)

        db_deleted: set[str] = set()
        db_kept: dict[str, dict] = {}

        for r in recent:
            mid = r.get("message_id", "")
            if r.get("decision") == "delete":
                db_deleted.add(mid)
            elif r.get("decision") in ("keep", "review"):
                db_kept[mid] = r

        # Проверяем 1: письма, которые мы "удалили", но их нет в корзине
        not_in_trash = db_deleted - trash_ids
        for msg_id in not_in_trash:
            try:
                msg = (
                    service.users().messages()
                    .get(userId="me", id=msg_id, format="metadata")
                    .execute()
                )
                sender = _get_header(msg, "from")
                subject = _get_header(msg, "subject")

                # Если письмо не в корзине и не удалено — пользователь восстановил
                if not _is_in_spam_or_inbox(service, msg_id):
                    stats["corrections"] += 1
                    stats["details"].append({
                        "type": "restored",
                        "msg_id": msg_id,
                        "subject": subject,
                        "sender": sender,
                    })
                    record_correction(
                        message_id=msg_id,
                        sender=sender,
                        original_decision="delete",
                        corrected_decision="keep",
                        source="trash_scan",
                    )
                    _update_sender_rule(sender, "keep")
                    print(f"  🔄 Восстановлено: {subject[:60]} ({sender[:40]})")
            except Exception as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    # Письмо удалено окончательно — всё в порядке
                    pass
                else:
                    stats["errors"] += 1
                    print(f"  ⚠️  Ошибка при проверке {msg_id}: {e}")

            stats["checked"] += 1

        # Проверяем 2: письма, которые мы "оставили", но они в корзине
        wrong_in_trash = trash_ids & set(db_kept.keys())
        for msg_id in wrong_in_trash:
            try:
                msg = (
                    service.users().messages()
                    .get(userId="me", id=msg_id, format="metadata")
                    .execute()
                )
                sender = _get_header(msg, "from")
                subject = _get_header(msg, "subject")

                stats["corrections"] += 1
                stats["details"].append({
                    "type": "manually_deleted",
                    "msg_id": msg_id,
                    "subject": subject,
                    "sender": sender,
                })
                record_correction(
                    message_id=msg_id,
                    sender=sender,
                    original_decision=db_kept[msg_id].get("decision", "unknown"),
                    corrected_decision="delete",
                    source="trash_scan",
                )
                _update_sender_rule(sender, "delete")
                print(f"  🔄 Вручную удалено: {subject[:60]} ({sender[:40]})")
            except Exception as e:
                stats["errors"] += 1
                print(f"  ⚠️  Ошибка при проверке {msg_id}: {e}")

            stats["checked"] += 1

        print(f"\n📊 Итоги сканирования корзины:")
        print(f"   ✅ Проверено: {stats['checked']}")
        print(f"   🔄 Коррекций: {stats['corrections']}")
        if stats["errors"]:
            print(f"   ⚠️  Ошибок: {stats['errors']}")

    except Exception as e:
        print(f"  ⚠️  Ошибка сканирования корзины: {e}")
        stats["errors"] += 1

    return stats


def _is_in_spam_or_inbox(service, msg_id: str) -> bool:
    """Проверяет, находится ли письмо в спаме или инбоксе."""
    try:
        msg = (
            service.users().messages()
            .get(userId="me", id=msg_id, format="minimal")
            .execute()
        )
        labels = msg.get("labelIds", [])
        return "SPAM" in labels or "INBOX" in labels or "CATEGORY_PRIMARY" in labels
    except Exception:
        return False


def _update_sender_rule(sender: str, decision: str) -> None:
    """Обновляет правило для отправителя на основе коррекции."""
    if not sender:
        return

    domain_match = re.search(r"@([\w.-]+)", sender)
    if not domain_match:
        return

    domain = domain_match.group(1).lower()

    # Сначала обновляем точный email, потом домен
    for pattern in [sender, f"@{domain}"]:
        try:
            set_sender_rule(pattern, decision, is_auto=1)
        except Exception:
            pass


def scan_and_report() -> None:
    """Удобная обёртка: сканирует корзину и выводит отчёт."""
    result = scan_trash()
    if result["corrections"] > 0:
        print(f"\n📋 Детали коррекций:")
        for d in result["details"]:
            emoji = "🔁" if d["type"] == "restored" else "🗑️"
            print(f"  {emoji} {d.get('subject', '—')[:60]} — {d.get('sender', '—')[:40]}")
    print()
