"""
Работа с Gmail API: получение писем, удаление, архивация.
"""

from __future__ import annotations

import base64
import email
from email.message import Message
from typing import Any

from googleapiclient.discovery import build, Resource

from auth import get_gmail_credentials


def get_service() -> Resource | None:
    """Создаёт и возвращает Gmail API сервис."""
    creds = get_gmail_credentials()
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds)


def _decode_body(payload: dict) -> str:
    """Рекурсивно достаёт текст письма из payload."""
    parts: list[dict[str, Any]] = []
    data = payload.get("body", {}).get("data", "")

    if data:
        parts.append(base64.urlsafe_b64decode(data).decode("utf-8", errors="replace"))

    for part in payload.get("parts", []):
        parts.append(_decode_body(part))

    return "\n".join(parts)


def _get_email_text(msg: dict) -> str:
    """Извлекает читаемый текст письма из raw-данных Gmail API."""
    headers = {
        h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])
    }
    subject = headers.get("subject", "(без темы)")
    sender = headers.get("from", "(неизвестно)")

    body = _decode_body(msg["payload"]).strip()
    # Если тело пустое — попробуем забрать из data_raw
    if not body:
        raw = msg.get("raw", "")
        if raw:
            decoded = base64.urlsafe_b64decode(raw)
            mime_msg: Message = email.message_from_bytes(decoded)
            if mime_msg.is_multipart():
                for part in mime_msg.walk():
                    ct = part.get_content_type()
                    if ct == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")  # type: ignore
                        break

    return f"From: {sender}\nSubject: {subject}\n\n{body[:5000]}"  # обрезаем до 5000 знаков


def fetch_unread(service: Resource, max_results: int = 20) -> list[dict]:
    """Достаёт непрочитанные письма из Inbox."""
    results = (
        service.users()
        .messages()
        .list(userId="me", q="is:unread in:inbox", maxResults=max_results)
        .execute()
    )
    messages = results.get("messages", [])
    if not messages:
        return []

    full_messages = []
    for msg_meta in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_meta["id"], format="full")
            .execute()
        )
        full_messages.append(msg)

    return full_messages


def fetch_all_inbox_ids(service: Resource, page_size: int = 200) -> list[str]:
    """Достаёт ID всех писем из Inbox. Быстро — только метаданные."""
    all_ids: list[str] = []
    page_token: str | None = None

    while True:
        results = (
            service.users()
            .messages()
            .list(userId="me", q="in:inbox", maxResults=page_size, pageToken=page_token)
            .execute()
        )
        batch = results.get("messages", [])
        if not batch:
            break
        for m in batch:
            all_ids.append(m["id"])
        page_token = results.get("nextPageToken")
        if not page_token:
            break
        print(f"  📥 Найдено писем: {len(all_ids)}", end="\r")

    print(f"  📥 Всего найдено: {len(all_ids)} писем")
    return all_ids


def fetch_message(service: Resource, msg_id: str) -> dict | None:
    """Загружает одно письмо по ID."""
    try:
        return (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )
    except Exception as e:
        print(f"  ⚠️  Ошибка загрузки письма {msg_id}: {e}")
        return None


# Маппинг меток классификатора → Gmail label names
_LABEL_MAP: dict[str, str] = {
    "personal": "Personal",
    "work": "Work",
    "receipt": "Receipts",
    "notification": "Notifications",
    "social": "Social",
    "marketing": "Marketing",
    "newsletter": "Newsletters",
}

# Кэш созданных меток {label_name: label_id}
_label_cache: dict[str, str] = {}


def get_or_create_label(service: Resource, label_name: str) -> str | None:
    """Находит или создаёт Gmail-метку. Возвращает labelId."""
    if label_name in _label_cache:
        return _label_cache[label_name]

    try:
        # Ищем среди существующих
        labels = service.users().labels().list(userId="me").execute()
        for lbl in labels.get("labels", []):
            if lbl["name"].lower() == label_name.lower():
                _label_cache[label_name] = lbl["id"]
                return lbl["id"]

        # Создаём новую
        created = (
            service.users()
            .labels()
            .create(
                userId="me",
                body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
            )
            .execute()
        )
        label_id = created["id"]
        _label_cache[label_name] = label_id
        return label_id
    except Exception as e:
        print(f"  ⚠️  Ошибка работы с меткой '{label_name}': {e}")
        return None


def add_label(service: Resource, msg_id: str, label_name: str) -> None:
    """Добавляет метку на письмо."""
    label_id = get_or_create_label(service, label_name)
    if not label_id:
        return
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": [label_id]},
        ).execute()
    except Exception as e:
        print(f"  ⚠️  Ошибка добавления метки '{label_name}': {e}")


def delete_message(service: Resource, msg_id: str) -> None:
    """Перемещает письмо в корзину (можно восстановить в течение 30 дней)."""
    service.users().messages().trash(userId="me", id=msg_id).execute()


def archive_message(service: Resource, msg_id: str) -> None:
    """Убирает метку Inbox (архивирует) и помечает прочитанным."""
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={
            "removeLabelIds": ["INBOX", "UNREAD"],
            "addLabelIds": [],
        },
    ).execute()


def keep_message(service: Resource, msg_id: str) -> None:
    """Помечает письмо как важное, оставляет во входящих."""
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={
            "addLabelIds": ["IMPORTANT", "STARRED"],
            "removeLabelIds": ["UNREAD"],
        },
    ).execute()
