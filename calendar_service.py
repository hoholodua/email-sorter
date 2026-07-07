"""
Google Calendar интеграция — создание событий из писем.
Парсит дату/время/место из текста письма (если есть) и создаёт событие в календаре.

Использует тот же OAuth-токен, что и Gmail API (с дополнительным scope).
"""

from __future__ import annotations

import re
import json
from datetime import datetime, timedelta
from typing import Any

from googleapiclient.discovery import build, Resource
from auth import get_gmail_credentials


def get_calendar_service() -> Resource | None:
    """Создаёт и возвращает Google Calendar API сервис."""
    creds = get_gmail_credentials()
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


def extract_event_info(email_text: str, subject: str) -> dict[str, Any] | None:
    """
    Пытается извлечь информацию о событии из письма.

    Ищет:
    - Дату/время в свободном формате
    - Ссылки на встречи
    - Место проведения

    Возвращает {"summary", "description", "start", "end", "location"} или None.
    """
    info: dict[str, Any] = {}
    text_lower = email_text.lower()

    # Ищем даты (грубый поиск)
    date_patterns = [
        r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})",
        r"(\d{4})-(\d{1,2})-(\d{1,2})",
        r"(понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресень[ея])",
        r"(tomorrow|today|next week|next month)",
        r"(\d{1,2}):(\d{2})\s*(am|pm)?",
        r"(\d{1,2}):(\d{2})",
    ]

    found_date = None
    found_time = None
    for pattern in date_patterns:
        match = re.search(pattern, text_lower)
        if match:
            if re.match(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}", match.group()):
                found_date = match.group()
            elif ":" in match.group():
                found_time = match.group()

    # Ищем Zoom/Meet/Teams ссылки
    link_patterns = [
        r"https?://zoom\.us/[^\s<>]+",
        r"https?://meet\.google\.com/[^\s<>]+",
        r"https?://teams\.microsoft\.com/[^\s<>]+",
    ]
    links = []
    for pattern in link_patterns:
        links.extend(re.findall(pattern, email_text))

    # Ищем локацию
    location = ""
    loc_patterns = [
        r"(?:адрес|address|location|place|место)\s*[:\s]+([^\n.]+)",
        r"(?:кабинет|офис|office|room)\s+([^\n.]+)",
    ]
    for pattern in loc_patterns:
        match = re.search(pattern, email_text, re.IGNORECASE)
        if match:
            location = match.group(1).strip()
            break

    # Если ничего не нашли — не создаём событие
    if not found_date and not links and not location:
        return None

    info["summary"] = f"📧 {subject[:100]}"
    info["description"] = f"Из письма: {subject}\n\n{email_text[:1000]}"

    if links:
        info["description"] += "\n\nСсылки:\n" + "\n".join(links)

    if location:
        info["location"] = location

    # Ставим событие на завтра (если дата не найдена) на 1 час
    tomorrow = datetime.now() + timedelta(days=1)
    start = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
    info["start"] = {"dateTime": start.isoformat(), "timeZone": "Europe/Moscow"}
    info["end"] = {"dateTime": (start + timedelta(hours=1)).isoformat(), "timeZone": "Europe/Moscow"}

    return info


def create_event(service: Resource, event_info: dict[str, Any]) -> bool:
    """Создаёт событие в календаре. Возвращает True при успехе."""
    try:
        body = {
            "summary": event_info["summary"],
            "description": event_info.get("description", ""),
            "start": event_info.get("start"),
            "end": event_info.get("end"),
        }
        if event_info.get("location"):
            body["location"] = event_info["location"]

        service.events().insert(calendarId="primary", body=body).execute()
        return True
    except Exception as e:
        print(f"  ⚠️  Ошибка создания события: {e}")
        return False


def try_create_event_from_email(email_text: str, subject: str, sender: str) -> bool:
    """
    Пытается создать событие в календаре из письма.
    Возвращает True если событие создано.
    """
    event_info = extract_event_info(email_text, subject)
    if not event_info:
        return False

    service = get_calendar_service()
    if not service:
        return False

    # Проверяем, что письмо не от рассылки (для них не создаём события)
    spammy_domains = ["mailgun.org", "sendgrid.net", "substack.com", "mailchimp.com",
                       "constantcontact.com", "marketo.com"]
    for d in spammy_domains:
        if d in sender.lower():
            return False

    if create_event(service, event_info):
        print(f"  📅 Событие создано: {event_info['summary']}")
        return True
    return False
