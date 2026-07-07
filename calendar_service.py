"""
Google Calendar интеграция — создание событий из писем.
Парсит дату/время/место из текста письма через LLM (Ollama).

Использует тот же OAuth-токен, что и Gmail API (с дополнительным scope).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from googleapiclient.discovery import build, Resource

from auth import get_gmail_credentials
from config import Config


EVENT_PROMPT = """Extract event information from this email if it contains a meeting, appointment, deadline, or scheduled event.

Look for:
- Meeting/event title
- Date and time
- Duration (if mentioned)
- Location or meeting link (Zoom, Google Meet, Teams, etc.)
- Organizer

Respond with JSON:
{{"is_event": true, "title": "event title", "date": "YYYY-MM-DD or empty", "time": "HH:MM or empty", "duration_minutes": 60, "location": "address/URL or empty", "description": "brief note or empty"}}
or
{{"is_event": false}}

Email:
Subject: {subject}
From: {sender}
Body: {text}"""


def _llm_extract_event(email_text: str, subject: str,
                        sender: str) -> dict[str, Any] | None:
    """Пытается извлечь данные о событии через LLM."""
    try:
        text_block = f"Subject: {subject}\nFrom: {sender}\nBody: {email_text[:2000]}"
        prompt = EVENT_PROMPT.format(subject=subject, sender=sender, text=text_block)

        resp = requests.post(
            f"{Config.OLLAMA_URL}/api/generate",
            json={
                "model": Config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "temperature": 0.1,
                "max_tokens": 200,
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        data = json.loads(raw)
        if data.get("is_event"):
            return data
    except Exception:
        pass
    return None


def get_calendar_service() -> Resource | None:
    """Создаёт и возвращает Google Calendar API сервис."""
    creds = get_gmail_credentials()
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


def _parse_event_datetime(event_data: dict) -> tuple[dict, dict] | None:
    """
    Преобразует данные из LLM в start/end для Google Calendar API.
    Возвращает (start, end) или None если дату определить не удалось.
    """
    date_str = event_data.get("date", "").strip()
    time_str = event_data.get("time", "").strip()
    duration = event_data.get("duration_minutes", 60)
    if not isinstance(duration, (int, float)) or duration < 15:
        duration = 60

    now = datetime.now(timezone(timedelta(hours=3)))  # Moscow time

    if date_str:
        # Пробуем разные форматы
        for fmt in ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"]:
            try:
                dt = datetime.strptime(date_str, fmt)
                break
            except ValueError:
                continue
        else:
            # Не смогли распарсить дату — игнорируем
            return None
    else:
        # Даты нет — не создаём событие
        return None

    # Если время указано — ставим его, иначе all-day
    if time_str:
        try:
            hour, minute = map(int, time_str.split(":"))
            start_dt = dt.replace(hour=hour, minute=minute)
        except (ValueError, TypeError):
            start_dt = dt.replace(hour=10, minute=0)
    else:
        # All-day event
        return (
            {"date": dt.date().isoformat()},
            {"date": (dt + timedelta(days=1)).date().isoformat()},
        )

    end_dt = start_dt + timedelta(minutes=duration)

    tz = "Europe/Moscow"
    return (
        {"dateTime": start_dt.isoformat(), "timeZone": tz},
        {"dateTime": end_dt.isoformat(), "timeZone": tz},
    )


def create_event(service: Resource, event_info: dict[str, Any],
                 event_data: dict[str, Any] | None = None) -> bool:
    """Создаёт событие в календаре. Возвращает True при успехе."""
    try:
        body = {
            "summary": event_info.get("summary", "Событие"),
            "description": event_info.get("description", ""),
        }
        if event_info.get("start"):
            body["start"] = event_info["start"]
        if event_info.get("end"):
            body["end"] = event_info["end"]
        if event_info.get("location"):
            body["location"] = event_info["location"]

        # Добавляем напоминание за 30 минут
        body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 30},
                {"method": "email", "minutes": 30},
            ],
        }

        service.events().insert(calendarId="primary", body=body).execute()
        return True
    except Exception as e:
        print(f"  ⚠️  Ошибка создания события: {e}")
        return False


def try_create_event_from_email(email_text: str, subject: str,
                                 sender: str) -> bool:
    """
    Пытается создать событие в календаре из письма.
    Использует LLM для извлечения данных о событии.

    Returns:
        True если событие создано.
    """
    # Пропускаем не-событийные письма по ключевым словам
    text_lower = f"{subject} {email_text[:500]}".lower()
    event_keywords = [
        "meeting", "встреч", "созвон", "конференц", "call",
        "встретиться", "appointment", "deadline", "дедлайн",
        "reminder", "напомина", "webinar", "вебинар",
        "interview", "собеседова", "workshop", "воркшоп",
        "мероприят", "event", "приглаш",
    ]
    if not any(kw in text_lower for kw in event_keywords):
        return False

    # Пропускаем рассылки
    spammy_domains = [
        "mailgun.org", "sendgrid.net", "substack.com",
        "mailchimp.com", "constantcontact.com", "marketo.com",
    ]
    for d in spammy_domains:
        if d in sender.lower():
            return False

    # LLM-извлечение
    event_data = _llm_extract_event(email_text, subject, sender)
    if not event_data:
        return False

    title = event_data.get("title", "").strip() or subject[:100]
    location = event_data.get("location", "") or ""
    description = event_data.get("description", "") or ""

    # Парсим дату/время
    times = _parse_event_datetime(event_data)
    if not times:
        return False

    start, end = times
    event_info = {
        "summary": f"📧 {title[:100]}",
        "description": (
            f"Из письма: {subject}\n"
            f"От: {sender}\n\n"
            f"{description}\n\n"
            f"---\n{email_text[:500]}"
        ),
        "start": start,
        "end": end,
    }
    if location:
        event_info["location"] = location

    service = get_calendar_service()
    if not service:
        return False

    if create_event(service, event_info, event_data):
        # Показываем человеку, что создали
        date_str = start.get("date", start.get("dateTime", "?"))
        print(f"  📅 Событие создано: {title[:60]} — {date_str}")
        return True
    return False
