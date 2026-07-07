#!/usr/bin/env python3
"""
Gmail Sorter — умная сортировка почты через локальную LLM (Ollama).

Использование:
  1. Настроить credentials (см. README)
  2. Создать .env
  3. python sorter.py              # одноразовый проход (непрочитанные)
     python sorter.py --all        # обработать ВСЮ почту в Inbox
     python sorter.py --watch      # проверка каждые N минут
     python sorter.py --listen     # IMAP IDLE — реакция на новые письма в реальном времени
     python sorter.py --dry-run    # классификация без выполнения действий (лог в .dry-run.json)
     python sorter.py --history    # показать историю обработки
     python sorter.py --digest     # отправить дневной дайджест
     python sorter.py --stats      # сгенерировать HTML-дашборд со статистикой
     python sorter.py --scan-trash # сканировать корзину на предмет коррекций
     python sorter.py --healthcheck # проверить IMAP-доступность

Новые модули версии 2.0:
  - dry-run        : классификация без применения действий к Gmail
  - calendar       : создание событий Google Calendar из писем
  - ghost-scanner  : авто-коррекция решений по содержимому Trash
  - stats          : HTML-дашборд с графиками (Chart.js)
  - macos-notifier : системные уведомления macOS + голосовые оповещения
  - healthcheck    : IMAP IDLE watchdog с мониторингом соединения
  - service-plugins: специализированные промпты для GitHub, банков и т.д.
  - extra_info     : детальные данные (сумма, PR#, магазин и т.д.)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, date, timedelta

from config import Config
from db import (
    record_decision,
    update_snippet,
    get_history,
    get_daily_stats,
    get_total_stats,
    already_processed,
    save_task,
    save_subscription,
    save_expense,
)
from probes import run_all_probes
from gmail_service import (
    get_service,
    fetch_unread,
    fetch_all_inbox_ids,
    fetch_message,
    delete_message,
    archive_message,
    keep_message,
    _get_email_text,
    add_label,
    _LABEL_MAP,
)
from classifier import classify_email
from imap_listener import GmailWatcher, run_healthcheck_cmd
from telegram_notifier import TelegramNotifier


def _get_notifier() -> TelegramNotifier:
    """Создаёт notifier из конфига (лениво, один раз)."""
    return TelegramNotifier(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)


def _get_header(msg: dict, name: str) -> str:
    """Достаёт заголовок из письма Gmail API."""
    headers = {
        h["name"].lower(): h["value"]
        for h in msg["payload"].get("headers", [])
    }
    return headers.get(name.lower(), "")


def process_email(service, msg_id: str, email_text: str, sender: str = "",
                  subject: str = "", notifier: TelegramNotifier | None = None,
                  dry_run: bool = False) -> str:
    """
    Классифицирует одно письмо и выполняет действие.

    Args:
        service: Gmail API сервис.
        msg_id: ID письма.
        email_text: Текст письма.
        sender: Отправитель.
        subject: Тема письма.
        notifier: Telegram-уведомитель (опционально).
        dry_run: Если True — только классифицировать, не выполнять действий.

    Returns:
        Строка с результатом.
    """
    result = classify_email(email_text, sender=sender)
    decision = result.get("decision", "keep")
    reason = result.get("reason", "")
    label = result.get("label", "")
    confidence = result.get("confidence", 0.0)
    urgency = result.get("urgency", "low")
    phishing_score = result.get("phishing_score", 0.0)
    extra_info = result.get("extra_info", {})

    action_map = {
        "keep": ("✅ Важное (keep)", lambda: keep_message(service, msg_id)),
        "archive": ("📦 Архив (archive)", lambda: archive_message(service, msg_id)),
        "delete": ("🗑️ Удалено (delete)", lambda: delete_message(service, msg_id)),
    }

    # Если уверенность ниже порога — переводим в review
    if confidence < Config.CONFIDENCE_THRESHOLD and confidence > 0:
        action = ("🤔 Требует проверки (review)", lambda: keep_message(service, msg_id))
        action_label = "review"
    else:
        action = action_map.get(decision)
        action_label = decision

    if action:
        label_str, doit = action

        # В dry-run не выполняем действий с Gmail API
        if not dry_run:
            doit()
        else:
            label_str = label_str.replace("✅", "🔮").replace("🗑️", "🔮").replace("📦", "🔮")

        # Умные метки (если включены) — только не в dry-run
        if not dry_run and Config.LABELS_ENABLED and label and label in _LABEL_MAP and decision != "delete":
            try:
                gmail_label = _LABEL_MAP[label]
                add_label(service, msg_id, gmail_label)
            except Exception:
                pass

        # Создание события в календаре (для важных писем и встреч)
        if not dry_run and decision == "keep":
            try:
                from calendar_service import try_create_event_from_email
                try_create_event_from_email(email_text, subject, sender)
            except ImportError:
                pass  # модуль не установлен — пропускаем
            except Exception as e:
                print(f"  ⚠️  Ошибка календаря: {e}")

        # macOS уведомления — только в не-dry-run
        if not dry_run:
            try:
                from notifier_macos import (
                    notify_deleted, notify_archived, notify_kept, notify_review, voice_alert
                )
                if action_label == "delete":
                    notify_deleted(subject, sender)
                    voice_alert("delete", subject, sender)
                elif action_label == "archive":
                    notify_archived(subject, sender)
                elif action_label == "keep":
                    notify_kept(subject, sender)
                elif action_label == "review":
                    notify_review(subject, sender, reason)
                    voice_alert("review", subject, sender)
            except ImportError:
                pass  # macOS-специфичный модуль может не быть доступен
            except Exception:
                pass

        # Запись в SQLite
        try:
            record_decision(
                message_id=msg_id,
                subject=subject[:200],
                sender=sender[:200],
                decision=decision,
                action=action_label,
                label=label,
                confidence=confidence,
                reason=reason,
                urgency=urgency,
                phishing_score=phishing_score,
                extra_info=json.dumps(extra_info, ensure_ascii=False),
            )
        except Exception:
            pass

        # Сохраняем сниппет для дайджеста (только для удалённых/архивированных)
        if action_label in ("delete", "archive") and email_text:
            try:
                # Чистим HTML и берём первые 500 символов
                import re as _re
                clean = _re.sub(r'<[^>]+>', ' ', email_text)
                clean = _re.sub(r'\s+', ' ', clean).strip()
                update_snippet(msg_id, clean[:500])
            except Exception:
                pass

        # ─── Probes: задачи, подписки, расходы ───────────────
        probes_results = run_all_probes(
            email_text, subject, sender, decision, extra_info,
        )

        # Сохраняем задачи
        for task in probes_results.get("tasks", []):
            try:
                save_task(
                    message_id=msg_id,
                    task=task.get("task", ""),
                    due=task.get("due", ""),
                    priority=task.get("priority", "medium"),
                )
            except Exception:
                pass

        # Сохраняем подписку
        sub = probes_results.get("subscription")
        if sub:
            try:
                save_subscription(
                    message_id=msg_id,
                    service=sub.get("service", sender),
                    amount=sub.get("amount", ""),
                    period=sub.get("period", "monthly"),
                    next_billing=sub.get("next_billing", ""),
                )
            except Exception:
                pass

        # Сохраняем расход
        expense = probes_results.get("expense")
        if expense:
            try:
                save_expense(
                    message_id=msg_id,
                    amount=expense.get("amount", ""),
                    currency=expense.get("currency", "RUB"),
                    merchant=expense.get("merchant", ""),
                    category=expense.get("category", "other"),
                )
            except Exception:
                pass

        # Telegram-уведомление о новой подписке
        if sub and notifier and notifier.enabled and not dry_run:
            try:
                amount_str = f" {sub['amount']}" if sub.get("amount") else ""
                text = (
                    f"🔄 <b>Обнаружена подписка</b>\n"
                    f"📧 {subject[:100]}\n"
                    f"👤 {sender[:80]}\n"
                    f"🏷️ {sub['service']}{amount_str}\n"
                    f"📅 Период: {sub['period']}"
                )
                if sub.get("next_billing"):
                    text += f"\n⏳ Следующее списание: {sub['next_billing']}"
                notifier.send(text)
            except Exception:
                pass

        # Telegram-уведомление о расходе (только крупные)
        if expense and notifier and notifier.enabled and not dry_run:
            try:
                try:
                    amt = float(expense['amount'])
                except (ValueError, TypeError):
                    amt = 0
                if amt >= 1000 or expense.get('category') in ('bills', 'entertainment'):
                    text = (
                        f"💰 <b>Расход</b>\n"
                        f"🏪 {expense['merchant']}\n"
                        f"💵 {expense['amount']} {expense['currency']}\n"
                        f"📂 {expense['category']}"
                    )
                    notifier.send(text)
            except Exception:
                pass

        # Telegram
        if notifier and notifier.enabled:
            try:
                # Фишинг-тревога — отдельное срочное уведомление
                if phishing_score >= 0.7 and not dry_run:
                    notifier.report_phishing_alert(subject, sender, phishing_score, reason)
                elif action_label == "review":
                    notifier.report_review(subject, sender, reason, confidence,
                                          extra_info=extra_info, urgency=urgency,
                                          msg_id=msg_id)
                elif dry_run:
                    notifier.report_dry_run(decision, subject, sender, reason,
                                           confidence, extra_info=extra_info)
                else:
                    notifier.report_processed(action_label, subject, sender, reason,
                                             extra_info=extra_info, urgency=urgency)
            except Exception:
                pass

        result_text = f"{label_str}: {reason}"
        if urgency == "high":
            result_text = "🔴 " + result_text.replace(": ", " **[СРОЧНО]** ")
        elif urgency == "medium":
            result_text = "🟡 " + result_text
        if extra_info:
            extra_parts = []
            for k, v in extra_info.items():
                if v:
                    extra_parts.append(f"{k}={v}")
            if extra_parts:
                result_text += f" [{', '.join(extra_parts)}]"
        if confidence > 0 and confidence < 1.0:
            result_text += f" (уверенность: {confidence:.0%})"

        # Probes в терминал
        for task in probes_results.get("tasks", []):
            p = task.get("priority", "medium")
            emoji = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(p, "⚪")
            result_text += f"\n   📋 Задача [{p}]: {task.get('task', '')}"
            if task.get("due"):
                result_text += f" (до {task['due']})"
        if sub:
            amount_str = f" {sub['amount']}" if sub.get("amount") else ""
            result_text += f"\n   🔄 Подписка: {sub['service']}{amount_str} ({sub['period']})"
        if expense:
            result_text += f"\n   💰 Расход: {expense['amount']} {expense['currency']} — {expense['merchant']} [{expense['category']}]"
        return result_text

    # fallback
    if not dry_run:
        keep_message(service, msg_id)
    return f"⚠️ Неизвестное решение '{decision}', письмо оставлено."


def _write_dry_run_log(results: list[dict]) -> None:
    """Сохраняет результаты dry-run в JSON-файл."""
    log_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(log_dir, exist_ok=True)
    filepath = os.path.join(log_dir, ".dry-run.json")

    existing = []
    if os.path.isfile(filepath):
        try:
            with open(filepath) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = []

    existing.extend(results)

    # Оставляем только последние 1000 записей
    existing = existing[-1000:]

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print(f"  💾 Лог dry-run сохранён: {filepath}")


# ─── Режимы запуска ───────────────────────────────────────


def run_dry_run() -> None:
    """Dry-run: классифицировать непрочитанные, НО НЕ выполнять действий."""
    errors = Config.validate()
    if errors:
        for e in errors:
            print(f"❌ {e}")
        sys.exit(1)

    service = get_service()
    if not service:
        print("❌ Не удалось подключиться к Gmail")
        return

    print(f"\n🔮 DRY RUN — классификация без применения действий")
    print(f"   Дата: {datetime.now(timezone.utc).isoformat()}")
    print(f"   Модель: {Config.OLLAMA_MODEL}")
    print(f"   Порог уверенности: {Config.CONFIDENCE_THRESHOLD}")
    print("=" * 60)

    messages = fetch_unread(service)
    if not messages:
        print("📭 Новых писем нет.")
        return

    total = len(messages)
    print(f"📨 Найдено писем: {total}")
    print("-" * 60)

    dry_results = []
    decisions_count: dict[str, int] = {}

    for msg in messages:
        try:
            email_text = _get_email_text(msg)
            sender = _get_header(msg, "from")
            subject = _get_header(msg, "subject")

            # Классифицируем (без действий)
            result = classify_email(email_text, sender=sender)
            decision = result.get("decision", "keep")
            reason = result.get("reason", "")
            label = result.get("label", "")
            confidence = result.get("confidence", 0.0)
            extra_info = result.get("extra_info", {})

            decisions_count[decision] = decisions_count.get(decision, 0) + 1

            emoji = {"keep": "✅", "archive": "📦", "delete": "🗑️"}
            actual_decision = decision
            if confidence < Config.CONFIDENCE_THRESHOLD and confidence > 0:
                actual_decision = "review"
                emoji["review"] = "🤔"

            result_line = (
                f"  {emoji.get(actual_decision, '❓')} [{actual_decision.upper()}] "
                f"{subject[:70]} | {sender[:40]}"
            )
            print(result_line)
            if reason:
                print(f"     Причина: {reason[:80]}")
            if extra_info:
                extra_str = ", ".join(f"{k}={v}" for k, v in extra_info.items() if v)
                if extra_str:
                    print(f"     Детали: {extra_str}")
            if confidence > 0:
                print(f"     Уверенность: {confidence:.0%}")
            print()

            dry_results.append({
                "subject": subject[:200],
                "sender": sender[:200],
                "decision": decision,
                "reason": reason,
                "label": label,
                "confidence": confidence,
                "extra_info": extra_info,
                "actual_decision": actual_decision,
            })
        except Exception as e:
            print(f"  ⚠️  Ошибка: {e}")

    # Сохраняем лог
    _write_dry_run_log(dry_results)

    # Итоги
    print("=" * 60)
    print("📊 Итоги DRY RUN:")
    for dec, cnt in sorted(decisions_count.items()):
        emoji = {"keep": "✅", "archive": "📦", "delete": "🗑️"}
        pct = cnt / total * 100 if total > 0 else 0
        print(f"   {emoji.get(dec, '❓')} {dec}: {cnt} ({pct:.0f}%)")
    print(f"\n   Всего проанализировано: {total}")


def run_once() -> None:
    """Один проход: забрать → классифицировать → обработать."""
    errors = Config.validate()
    if errors:
        for e in errors:
            print(f"❌ {e}")
        sys.exit(1)

    service = get_service()
    if not service:
        print("❌ Не удалось подключиться к Gmail")
        return

    print(f"\n🔍 Проверка почты... ({datetime.now(timezone.utc).isoformat()})")
    messages = fetch_unread(service)

    if not messages:
        print("📭 Новых писем нет.")
        return

    total = len(messages)
    deleted = 0
    kept = 0
    errors_count = 0

    print(f"📨 Найдено писем: {total}")
    print("-" * 60)

    for msg in messages:
        try:
            email_text = _get_email_text(msg)
            sender = _get_header(msg, "from")
            subject = _get_header(msg, "subject")

            # Показываем короткую инфу
            for line in email_text.split("\n")[:3]:
                print(f"  {line}")
            print(f"  ---")

            action_result = process_email(service, msg["id"], email_text, sender=sender, subject=subject)
            print(f"  {action_result}")

            if "delete" in action_result or "Удалено" in action_result:
                deleted += 1
            else:
                kept += 1

        except Exception as e:
            print(f"  ⚠️  Ошибка при обработке: {e}")
            errors_count += 1

        print("-" * 40)

    print(f"\n📊 Итоги: {total} писем, 🗑️ {deleted}, ✅ {kept}")
    if errors_count:
        print(f"   ⚠️  Ошибок: {errors_count}")

    # Telegram
    notifier = _get_notifier()
    if notifier.enabled and total > 0:
        notifier.report_summary(total, deleted, kept, errors_count, source="разовый проход")


def run_watch() -> None:
    """Запускает агента как сервис — проверяет каждые N минут."""
    print(f"🕐 Агент запущен. Проверка каждые {Config.CHECK_INTERVAL_MINUTES} мин.")
    print("   Нажми Ctrl+C для остановки.\n")

    while True:
        run_once()
        print(f"\n💤 Следующая проверка через {Config.CHECK_INTERVAL_MINUTES} мин...")
        try:
            time.sleep(Config.CHECK_INTERVAL_MINUTES * 60)
        except KeyboardInterrupt:
            print("\n👋 Остановлено.")
            break


def run_once_all() -> None:
    """Полная чистка: обрабатывает ВСЕ письма в Inbox."""
    errors = Config.validate()
    if errors:
        for e in errors:
            print(f"❌ {e}")
        sys.exit(1)

    service = get_service()
    if not service:
        print("❌ Не удалось подключиться к Gmail")
        return

    print(f"\n🔍 Полная чистка Inbox... ({datetime.now(timezone.utc).isoformat()})")
    print("⚠️  Собираю ID всех писем...")

    msg_ids = fetch_all_inbox_ids(service)

    if not msg_ids:
        print("📭 Inbox пуст.")
        return

    total = len(msg_ids)
    print(f"\n📨 Найдено писем: {total}")
    print("=" * 60)

    deleted = 0
    kept = 0
    errors_count = 0

    for i, msg_id in enumerate(msg_ids, 1):
        # Загружаем одно письмо
        msg = fetch_message(service, msg_id)
        if not msg:
            errors_count += 1
            continue

        try:
            email_text = _get_email_text(msg)
            sender = _get_header(msg, "from")
            subject = _get_header(msg, "subject")

            # Показываем краткую инфу
            for line in email_text.split("\n")[:2]:
                print(f"  {line}")
            print(f"  ---")

            action_result = process_email(service, msg["id"], email_text, sender=sender, subject=subject)
            print(f"  {action_result}")

            if "delete" in action_result or "Удалено" in action_result:
                deleted += 1
            else:
                kept += 1

            print(f"  [{i}/{total}]")
            print("-" * 40)

        except Exception as e:
            print(f"  ⚠️  Ошибка при обработке письма {msg_id}: {e}")
            errors_count += 1
            continue

    print("\n" + "=" * 60)
    print(f"📊 Итоги чистки:")
    print(f"   🗑️  Удалено: {deleted}")
    print(f"   ✅ Оставлено: {kept}")
    if errors_count:
        print(f"   ⚠️  Ошибок: {errors_count}")
    print("=" * 60)

    # Telegram
    notifier = _get_notifier()
    if notifier.enabled and total > 0:
        notifier.report_summary(total, deleted, kept, errors_count, source="полная чистка")


def send_digest() -> None:
    """Smart Digest: отправляет дайджест удалённых писем с LLM-саммари в Telegram."""
    from digest import send_digest as _send_digest

    notifier = _get_notifier()
    if not notifier.enabled:
        print("❌ Telegram не настроен")
        return

    today = date.today()
    result = _send_digest(notifier, target_date=today, dry_run=False)
    if result["total"] == 0:
        # Если сегодня ничего — пробуем вчера
        yesterday = today - timedelta(days=1)
        result = _send_digest(notifier, target_date=yesterday, dry_run=False)
    if result["total"] == 0:
        notifier.send("📋 <b>Дайджест Email Sorter</b>\n📅 Сегодня удалённых писем нет.")
        print("📭 Нет данных для дайджеста.")
    else:
        print(f"\n📊 Дайджест за {result['date']} отправлен: {result['total']} писем")


def show_history(limit: int = 30) -> None:
    """Показывает историю обработки."""
    records = get_history(limit=limit)
    if not records:
        print("📭 История пуста.")
        return

    print(f"\n📋 История обработки (последние {len(records)}):")
    print("=" * 80)
    for r in records:
        ts = r["processed_at"][:19] if r["processed_at"] else ""
        action_emoji = {"keep": "✅", "delete": "🗑️", "archive": "📦", "review": "🤔"}.get(r["action"], "❓")
        label_tag = f" [{r['label']}]" if r.get("label") else ""
        conf = f" ({r['confidence']:.0%})" if r.get("confidence", 0) > 0 else ""

        # Показываем extra_info если есть
        extra_str = ""
        if r.get("extra_info"):
            try:
                ei = json.loads(r["extra_info"])
                extra_parts = [f"{k}={v}" for k, v in ei.items() if v]
                if extra_parts:
                    extra_str = f" 🔍 {', '.join(extra_parts)}"
            except (json.JSONDecodeError, TypeError):
                pass

        print(f"  {ts} {action_emoji} {r['subject'][:60]:60s}{label_tag}{conf}")
        print(f"     {r['sender'][:60]:60s} | {r['reason'][:80]}{extra_str}")
    print("=" * 80)


def run_stats() -> None:
    """Генерирует HTML-дашборд со статистикой."""
    try:
        from stats import generate_dashboard, open_dashboard
        filepath = generate_dashboard()
        print(f"📊 Дашборд сгенерирован: file://{filepath}")
        print("   Открываю в браузере...")
        open_dashboard()
    except ImportError as e:
        print(f"❌ Модуль stats.py недоступен: {e}")
    except Exception as e:
        print(f"⚠️  Ошибка генерации дашборда: {e}")


def run_scan_trash() -> None:
    """Сканирует корзину на предмет авто-коррекций."""
    try:
        from ghost_scanner import scan_and_report
        scan_and_report()
    except ImportError as e:
        print(f"❌ Модуль ghost_scanner.py недоступен: {e}")
    except Exception as e:
        print(f"⚠️  Ошибка сканирования корзины: {e}")


def run_healthcheck() -> None:
    """Проверяет IMAP-доступность."""
    result = run_healthcheck_cmd()
    if result.get("status") == "ok":
        sys.exit(0)
    else:
        sys.exit(1)


def run_listen() -> None:
    """Режим реального времени: IMAP IDLE + обработка новых писем по сигналу."""
    errors = Config.validate()
    if errors:
        for e in errors:
            print(f"❌ {e}")
        sys.exit(1)

    if not Config.GMAIL_EMAIL:
        print("❌ Укажите GMAIL_EMAIL в .env")
        sys.exit(1)

    service = get_service()
    if not service:
        print("❌ Не удалось подключиться к Gmail")
        return

    print(f"\n🔔 Запуск в режиме реального времени (IMAP IDLE)")
    print(f"   Адрес: {Config.GMAIL_EMAIL}")
    print(f"   Модель: {Config.OLLAMA_MODEL}")
    print(f"   Метки: {'вкл' if Config.LABELS_ENABLED else 'выкл'}")
    print(f"   Порог уверенности: {Config.CONFIDENCE_THRESHOLD}")
    print(f"   Дайджест: в {Config.DIGEST_HOUR}:00")
    print(f"   Нажми Ctrl+C для остановки.\n")

    notifier = _get_notifier()
    if notifier.enabled:
        notifier.report_startup("IMAP IDLE — реальное время", Config.GMAIL_EMAIL, Config.OLLAMA_MODEL)

    # Сначала обрабатываем текущие непрочитанные
    pending = fetch_unread(service, max_results=50)
    if pending:
        deleted = 0
        kept = 0
        errors_count = 0
        print(f"📨 Непрочитанных писем: {len(pending)} — обрабатываю...")
        for msg in pending:
            try:
                email_text = _get_email_text(msg)
                sender = _get_header(msg, "from")
                subject = _get_header(msg, "subject")
                action_result = process_email(service, msg["id"], email_text, sender=sender, subject=subject,
                                              notifier=notifier)
                print(f"  {action_result}")
                if "delete" in action_result or "Удалено" in action_result:
                    deleted += 1
                else:
                    kept += 1
            except Exception as e:
                print(f"  ⚠️  Ошибка: {e}")
                errors_count += 1
        if notifier.enabled:
            notifier.report_summary(len(pending), deleted, kept, errors_count, source="непрочитанные при старте")
        print()

    # Запускаем IMAP IDLE слушатель с дайджестом и healthcheck
    last_digest_day: int | None = None

    def healthcheck_handler(status: str, details: str) -> None:
        """Обработчик healthcheck-событий."""
        if notifier and notifier.enabled:
            try:
                notifier.report_healthcheck(status, details)
            except Exception:
                pass

    def on_notification():
        """Вызывается при каждом сигнале IMAP IDLE о новом письме."""
        nonlocal last_digest_day

        try:
            # Проверка дайджеста (раз в день)
            today = datetime.now(timezone.utc).astimezone().day
            if last_digest_day is None or today != last_digest_day:
                last_digest_day = today
                # Проверяем, что час подходит
                local_hour = datetime.now(timezone.utc).astimezone().hour
                if local_hour >= Config.DIGEST_HOUR:
                    try:
                        from digest import send_digest as _smart_digest
                        _smart_digest(notifier, dry_run=False)
                        print(f"  📋 Smart дайджест отправлен")
                    except Exception as e:
                        print(f"  ⚠️  Ошибка дайджеста: {e}")

            messages = fetch_unread(service, max_results=10)
            if not messages:
                print("  📭 Сигнал получен, но новых писем нет.")
                return

            for msg in messages:
                try:
                    email_text = _get_email_text(msg)
                    sender = _get_header(msg, "from")
                    subject = _get_header(msg, "subject")
                    print(f"  📧 Новое письмо: {subject[:80]}")
                    print(f"     От: {sender[:60]}")
                    action_result = process_email(service, msg["id"], email_text, sender=sender, subject=subject,
                                                  notifier=notifier)
                    print(f"     {action_result}")

                except Exception as e:
                    print(f"  ⚠️  Ошибка при обработке: {e}")
                    if notifier.enabled:
                        notifier.report_error(f"Ошибка при обработке письма: {e}")

        except Exception as e:
            print(f"  ⚠️  Ошибка в обработчике уведомлений: {e}")

    # Периодическая проверка корзины (раз в час через счётчик IDLE-циклов)
    _trash_scan_counter = 0

    # Оборачиваем on_notification чтобы добавить периодический trash-scan
    original_on_notification = on_notification

    def wrapped_on_notification():
        nonlocal _trash_scan_counter
        _trash_scan_counter += 1

        # Каждые ~12 уведомлений (~5 часов при 25-минутном IDLE) делаем trash-scan
        if _trash_scan_counter % 12 == 0:
            try:
                from ghost_scanner import scan_trash
                print("  🔄 Периодическое сканирование корзины...")
                scan_trash(max_results=100)
            except ImportError:
                pass
            except Exception as e:
                print(f"  ⚠️  Ошибка trash-scan: {e}")

        original_on_notification()

    watcher = GmailWatcher(
        Config.GMAIL_EMAIL,
        on_notification=wrapped_on_notification,
        healthcheck_callback=healthcheck_handler,
    )
    try:
        watcher.run()
    except KeyboardInterrupt:
        print("\n👋 Остановлено.")


def run_telegram_poll() -> None:
    """Режим обработки Telegram callback-запросов (inline кнопки)."""
    from telegram_notifier import poll_telegram_callbacks
    from gmail_service import keep_message, delete_message, archive_message

    notifier = _get_notifier()
    if not notifier.enabled:
        print("❌ Telegram не настроен.")
        return

    service = get_service()
    if not service:
        print("❌ Не удалось подключиться к Gmail")
        return

    def callback_handler(data: str, extra: dict) -> None:
        """Обрабатывает нажатие кнопки в Telegram."""
        parts = data.split(":", 2)
        action = parts[0]
        msg_id = parts[1] if len(parts) > 1 else ""

        if action == "delete":
            result = delete_message(service, msg_id)
            print(f"  🗑️ Удалено {msg_id}")
        elif action == "keep":
            result = keep_message(service, msg_id)
            print(f"  ✅ Оставлено {msg_id}")
        elif action == "archive":
            result = archive_message(service, msg_id)
            print(f"  📦 В архив {msg_id}")
        else:
            print(f"  ⚠️ Неизвестное действие: {action}")

        # Обновляем сообщение в Telegram — убираем кнопки
        try:
            notifier.edit_message(
                extra.get("message_id", 0),
                f"✅ <b>Действие выполнено:</b> {action.upper()}"
            )
        except Exception:
            pass

    print("\n📡 Режим обработки Telegram callback-запросов")
    poll_telegram_callbacks(notifier, handler=callback_handler, single_run=False)


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        run_dry_run()
    elif "--digest" in sys.argv:
        send_digest()
    elif "--history" in sys.argv:
        show_history()
    elif "--listen" in sys.argv or "--idle" in sys.argv:
        run_listen()
    elif "--watch" in sys.argv:
        run_watch()
    elif "--all" in sys.argv:
        run_once_all()
    elif "--stats" in sys.argv:
        run_stats()
    elif "--scan-trash" in sys.argv:
        run_scan_trash()
    elif "--healthcheck" in sys.argv:
        run_healthcheck()
    elif "--telegram-poll" in sys.argv:
        run_telegram_poll()
    else:
        run_once()
