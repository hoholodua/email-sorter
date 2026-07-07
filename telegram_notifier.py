"""
Отправка уведомлений в Telegram через Bot API.
Поддерживает обычные сообщения, форматированные отчёты, и extra_info.

Теперь включает детали из писем:
- Суммы и магазины (банковские уведомления)
- PR и репозитории (GitHub)
- Номер заказа и статус (доставка)
- И т.д.

Поддерживает inline-кнопки (Approve/Decline) для сообщений,
требующих решения пользователя.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable
import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _format_extra_info(info: dict[str, Any]) -> str:
    """Форматирует extra_info в строку для Telegram."""
    if not info:
        return ""

    parts = []
    field_labels = {
        "amount": "💰 Сумма",
        "currency": "💱 Валюта",
        "merchant": "🏪 Магазин",
        "store": "🏪 Магазин",
        "transaction_type": "📋 Тип",
        "balance": "💳 Баланс",
        "order_number": "📦 Заказ",
        "item": "📦 Товар",
        "status": "📌 Статус",
        "pr_number": "🔀 PR",
        "repo": "📁 Репозиторий",
        "action": "🔧 Действие",
        "reviewer": "👁️ Ревьюер",
        "task_name": "📋 Задача",
        "project": "📁 Проект",
        "assignee": "👤 Исполнитель",
        "event_title": "📅 Событие",
        "event_time": "⏰ Время",
        "meeting_link": "🔗 Ссылка",
        "organizer": "👤 Организатор",
        "platform": "📱 Платформа",
        "user": "👤 Пользователь",
        "post_title": "📝 Заголовок",
        "ticket_number": "🎫 Тикет",
        "build_result": "⚙️ Результат",
        "package_name": "📦 Пакет",
        "version": "🔖 Версия",
        "delivery_status": "🚚 Статус доставки",
        "tracking_number": "🔢 Трек-номер",
    }

    for key, value in info.items():
        if value is None or value == "":
            continue
        label = field_labels.get(key, f"📎 {key.capitalize()}")
        parts.append(f"{label}: {value}")

    return "\n".join(parts) if parts else ""


class TelegramNotifier:
    """Отправляет сообщения в Telegram через Bot API."""

    # Хранилище: telegram_message_id -> {action: callback}
    _callbacks: dict[int, dict[str, Callable]] = {}
    # Смещение для getUpdates
    _last_update_id: int = 0

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _api_call(self, method: str, payload: dict) -> dict | None:
        """Базовый вызов Telegram Bot API."""
        if not self._enabled:
            return None
        try:
            resp = requests.post(
                TELEGRAM_API.format(token=self.bot_token, method=method),
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                print(f"  ⚠️  Telegram API: {data.get('description', 'unknown error')}")
                return None
            return data
        except requests.RequestException as e:
            print(f"  ⚠️  Telegram: ошибка: {e}")
            return None

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Отправляет произвольное сообщение. Возвращает True при успехе."""
        result = self._api_call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        })
        return result is not None

    def send_with_keyboard(
        self,
        text: str,
        buttons: list[list[dict[str, str]]],
        parse_mode: str = "HTML",
    ) -> int | None:
        """
        Отправляет сообщение с inline-кнопками.

        Args:
            text: Текст сообщения.
            buttons: Список рядов кнопок. Каждая кнопка: {"text": "...", "callback_data": "..."}
            parse_mode: HTML | Markdown

        Returns:
            Telegram message_id (int) или None при ошибке.
        """
        result = self._api_call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": b["text"], "callback_data": b["callback_data"]} for b in row]
                    for row in buttons
                ],
            },
        })
        if result and result.get("result", {}).get("message_id"):
            return result["result"]["message_id"]
        return None

    def register_callback(self, message_id: int, action: str, callback: Callable) -> None:
        """Регистрирует callback для inline-кнопки."""
        if message_id not in self._callbacks:
            self._callbacks[message_id] = {}
        self._callbacks[message_id][action] = callback

    def edit_message(self, message_id: int, text: str, parse_mode: str = "HTML") -> bool:
        """Обновляет текст существующего сообщения."""
        result = self._api_call("editMessageText", {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        })
        return result is not None

    def remove_keyboard(self, message_id: int) -> bool:
        """Убирает клавиатуру с сообщения."""
        result = self._api_call("editMessageReplyMarkup", {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })
        return result is not None

    def poll_updates(self, timeout: int = 30) -> list[dict]:
        """
        Long-poll для получения обновлений (callback queries).

        Returns:
            Список обработанных апдейтов.
        """
        result = self._api_call("getUpdates", {
            "offset": self._last_update_id + 1,
            "timeout": timeout,
            "allowed_updates": ["callback_query"],
        })
        if not result:
            return []
        updates = result.get("result", [])
        if updates:
            self._last_update_id = updates[-1]["update_id"]
        return updates

    def handle_callback(self, update: dict) -> bool:
        """
        Обрабатывает один callback_query от inline-кнопки.

        Returns:
            True если callback был обработан.
        """
        cq = update.get("callback_query", {})
        if not cq:
            return False

        data = cq.get("data", "")
        message = cq.get("message", {})
        message_id = message.get("message_id")
        callback_id = cq.get("id")

        if not message_id or not data:
            return False

        # Разбираем callback_data: "action:msg_id:param"
        parts = data.split(":", 2)
        action = parts[0]

        # Отвечаем на callback (убираем "часики" у кнопки)
        self._api_call("answerCallbackQuery", {
            "callback_query_id": callback_id,
        })

        # Ищем зарегистрированный callback
        if message_id in self._callbacks and action in self._callbacks[message_id]:
            try:
                self._callbacks[message_id][action]()
                return True
            except Exception as e:
                print(f"  ⚠️  Ошибка обработки callback {action}: {e}")
                return False

        return False

    def report_processed(self, action: str, subject: str, sender: str, reason: str,
                         extra_info: dict[str, Any] | None = None,
                         urgency: str = "low") -> None:
        """Короткое уведомление об одном обработанном письме."""
        if not self._enabled:
            return
        emoji = {
            "delete": "🗑️",
            "archive": "📦",
            "keep": "✅",
        }.get(action, "📝")
        urgency_tag = ""
        if urgency == "high":
            urgency_tag = " 🔴 СРОЧНО"
        elif urgency == "medium":
            urgency_tag = " 🟡 Важно"
        text = (
            f"{emoji} <b>Письмо обработано{urgency_tag}</b>\n"
            f"📧 {subject[:100]}\n"
            f"👤 {sender[:80]}\n"
            f"<b>{action.upper()}</b>: {reason[:200]}"
        )

        # Добавляем extra_info если есть
        if extra_info:
            extra_block = _format_extra_info(extra_info)
            if extra_block:
                text += f"\n\n{extra_block}"

        self.send(text)

    def report_summary(self, processed: int, deleted: int, kept: int,
                       errors: int = 0, source: str = "",
                       extra_summary: str = "") -> None:
        """Итоговый отчёт после батча обработки."""
        src_tag = f"— {source}" if source else ""
        text = (
            f"📬 <b>Email Sorter {src_tag}</b>\n"
            f"Обработано: {processed}\n"
            f"🗑️ Удалено: {deleted}\n"
            f"✅ Оставлено: {kept}\n"
        )
        if errors:
            text += f"⚠️ Ошибок: {errors}"
        if extra_summary:
            text += f"\n{extra_summary}"
        self.send(text)

    def report_startup(self, mode: str, email: str, model: str,
                       extra_info: str = "") -> None:
        """Уведомление о запуске."""
        text = (
            f"🚀 <b>Email Sorter запущен</b>\n"
            f"Режим: {mode}\n"
            f"📧 {email}\n"
            f"🧠 {model}"
        )
        if extra_info:
            text += f"\n\n{extra_info}"
        self.send(text)

    def report_error(self, msg: str) -> None:
        """Уведомление об ошибке."""
        text = f"⚠️ <b>Ошибка</b>\n{msg[:300]}"
        self.send(text)

    def report_phishing_alert(self, subject: str, sender: str,
                               phishing_score: float, reason: str) -> None:
        """Срочное уведомление о подозрительном письме (фишинг)."""
        score_pct = phishing_score * 100
        text = (
            f"🚨 <b>Заподозрен фишинг!</b>\n"
            f"📧 {subject[:100]}\n"
            f"👤 {sender[:80]}\n"
            f"🎯 Уверенность: {score_pct:.0f}%\n"
            f"📝 {reason[:300]}\n\n"
            f"⚠️ Не переходите по ссылкам и не отвечайте!"
        )
        self.send(text)

    def report_review(self, subject: str, sender: str, reason: str,
                      confidence: float, extra_info: dict[str, Any] | None = None,
                      urgency: str = "low", msg_id: str = "") -> None:
        """Письмо требует проверки (низкая уверенность) — с кнопками решения."""
        urgency_tag = ""
        if urgency == "high":
            urgency_tag = " 🔴 СРОЧНО"
        elif urgency == "medium":
            urgency_tag = " 🟡 Важно"
        text = (
            f"🤔 <b>Требуется проверка{urgency_tag}</b>\n"
            f"📧 {subject[:100]}\n"
            f"👤 {sender[:80]}\n"
            f"Уверенность: {confidence:.0%}\n"
            f"Причина: {reason[:200]}"
        )
        if extra_info:
            extra_block = _format_extra_info(extra_info)
            if extra_block:
                text += f"\n\n{extra_block}"

        # Callback_data включает message_id для связи с email
        mid_suffix = f":{msg_id}" if msg_id else ""
        buttons = [
            [
                {"text": "🗑️ Удалить", "callback_data": f"delete{mid_suffix}"},
                {"text": "✅ Оставить", "callback_data": f"keep{mid_suffix}"},
                {"text": "📦 В архив", "callback_data": f"archive{mid_suffix}"},
            ],
        ]

        msg_id = self.send_with_keyboard(text, buttons)

    def report_digest(self, stats: dict) -> None:
        """Ежедневный дайджест."""
        labels_block = ""
        if stats.get("labels"):
            labels_block = "\n".join(
                f"  • {lbl}: {cnt}" for lbl, cnt in list(stats["labels"].items())[:8]
            )
            labels_block = f"\n{labels_block}"

        text = (
            f"📊 <b>Дайджест Email Sorter</b>\n"
            f"📅 {stats['date']}\n\n"
            f"Всего обработано: <b>{stats['total']}</b>\n"
            f"🗑️ Удалено: {stats['deleted']}\n"
            f"📦 В архив: {stats['archived']}\n"
            f"✅ Оставлено: {stats['kept']}\n"
        )
        if stats.get("low_confidence"):
            text += f"🤔 Требуют проверки: {stats['low_confidence']}\n"
        if labels_block:
            text += f"\n<b>По типам:</b>{labels_block}"
        self.send(text)

    def report_correction(self, subject: str, sender: str,
                          original: str, corrected: str) -> None:
        """Уведомление о коррекции решения (перемещение/восстановление)."""
        emoji = "🔄" if corrected == "keep" else "📤"
        text = (
            f"{emoji} <b>Коррекция решения</b>\n"
            f"📧 {subject[:100]}\n"
            f"👤 {sender[:80]}\n"
            f"Было: {original.upper()} → Стало: {corrected.upper()}"
        )
        self.send(text)

    def report_healthcheck(self, status: str, details: str = "") -> None:
        """Уведомление о здоровье системы."""
        emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
        text = f"{emoji} <b>Healthcheck</b>\nСтатус: {status.upper()}"
        if details:
            text += f"\n{details[:200]}"
        self.send(text)

    def report_dry_run(self, decision: str, subject: str, sender: str,
                       reason: str, confidence: float,
                       extra_info: dict[str, Any] | None = None) -> None:
        """Уведомление о результате dry-run (без выполнения действия)."""
        emoji = {"keep": "✅", "archive": "📦", "delete": "🗑️"}.get(decision, "🤔")
        text = (
            f"🔮 <b>Dry Run: {decision.upper()}</b>\n"
            f"📧 {subject[:100]}\n"
            f"👤 {sender[:80]}\n"
            f"Причина: {reason[:200]}\n"
            f"Уверенность: {confidence:.0%}"
        )
        if extra_info:
            extra_block = _format_extra_info(extra_info)
            if extra_block:
                text += f"\n\n{extra_block}"
        self.send(text)


def poll_telegram_callbacks(notifier: TelegramNotifier,
                             handler: Callable[[str, dict], None] | None = None,
                             single_run: bool = False) -> None:
    """
    Цикл обработки callback-запросов от inline-кнопок.

    Args:
        notifier: Экземпляр TelegramNotifier.
        handler: Функция обратного вызова: handler(action, extra_data).
                 Если None — использует стандартную обработку.
        single_run: Если True — один опрос и выход. Иначе бесконечный цикл.
    """
    if not notifier.enabled:
        print("  ⚠️ Telegram не настроен, пропускаем poll.")
        return

    print("  📡 Запуск обработки Telegram callback-запросов...")

    while True:
        try:
            updates = notifier.poll_updates(timeout=10)
            for update in updates:
                cq = update.get("callback_query", {})
                data = cq.get("data", "")
                message = cq.get("message", {})

                if handler:
                    handler(data, {
                        "message_id": message.get("message_id"),
                        "chat_id": message.get("chat", {}).get("id"),
                        "from": cq.get("from", {}),
                    })
                else:
                    notifier.handle_callback(update)

            if single_run:
                break

        except KeyboardInterrupt:
            print("\n  👋 Остановка poll.")
            break
        except Exception as e:
            print(f"  ⚠️ Ошибка poll: {e}")
            time.sleep(5)
