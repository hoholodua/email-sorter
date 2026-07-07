"""
macOS уведомления через osascript + голосовые оповещения через say.

Использует нативные встроенные средства macOS без внешних зависимостей:
- osascript — для системных уведомлений (Notification Center)
- say — для голосовых оповещений (Text-to-Speech)
"""

from __future__ import annotations

import subprocess
import shlex
from typing import Any


def send_notification(title: str, message: str, sound: bool = True) -> bool:
    """
    Отправляет системное уведомление macOS через osascript.

    Args:
        title: Заголовок уведомления.
        message: Текст уведомления.
        sound: Воспроизвести звук (по умолчанию True).

    Returns:
        True если уведомление отправлено.
    """
    try:
        sound_cmd = 'sound name "default"' if sound else ""
        script = f'display notification {shlex.quote(message)} with title {shlex.quote(title)} {sound_cmd}'
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
        return True
    except Exception as e:
        print(f"  ⚠️  Ошибка macOS-уведомления: {e}")
        return False


def speak_text(text: str, voice: str = "", rate: int = 200) -> bool:
    """
    Голосовое оповещение через команду 'say'.

    Args:
        text: Текст для озвучивания.
        voice: Голос (например "Milena" для русской речи, "Anna" для английской).
               Пустая строка = голос по умолчанию.
        rate: Скорость речи (слов в минуту, по умолчанию 200).

    Returns:
        True если озвучивание выполнено.
    """
    try:
        cmd = ["say"]
        if voice:
            cmd.extend(["-v", voice])
        cmd.extend(["-r", str(rate)])
        cmd.append(text)
        subprocess.run(cmd, capture_output=True, timeout=30)
        return True
    except Exception as e:
        print(f"  ⚠️  Ошибка голосового оповещения: {e}")
        return False


def notify_deleted(subject: str, sender: str = "") -> None:
    """Уведомление об удалении письма."""
    msg = f"🗑️ Удалено: {subject[:80]}"
    if sender:
        msg += f" от {sender[:40]}"
    send_notification("Email Sorter — Удаление", msg)


def notify_archived(subject: str, sender: str = "") -> None:
    """Уведомление об архивации письма."""
    msg = f"📦 В архив: {subject[:80]}"
    if sender:
        msg += f" от {sender[:40]}"
    send_notification("Email Sorter — Архив", msg)


def notify_kept(subject: str, sender: str = "") -> None:
    """Уведомление о важном письме."""
    msg = f"✅ Важное: {subject[:80]}"
    if sender:
        msg += f" от {sender[:40]}"
    send_notification("Email Sorter — Важное", msg)


def notify_review(subject: str, sender: str = "", reason: str = "") -> None:
    """Уведомление о письме, требующем проверки."""
    msg = f"🤔 Требует проверки: {subject[:60]}"
    if sender:
        msg += f" от {sender[:30]}"
    if reason:
        msg += f" — {reason[:80]}"
    send_notification("Email Sorter — Внимание", msg)


def notify_error(message: str) -> None:
    """Уведомление об ошибке."""
    send_notification("⚠️ Email Sorter — Ошибка", message[:120])


def voice_alert(decision: str, subject: str, sender: str = "") -> None:
    """
    Голосовое оповещение (say) о важных действиях.

    Срабатывает только для delete (неожиданные удаления) и review (требует внимания).
    """
    if decision == "delete":
        text = f"Внимание! Удалено письмо: {subject[:60]}"
        speak_text(text, voice="Milena", rate=190)
    elif decision == "review":
        text = f"Требуется ваше внимание. Письмо: {subject[:60]}"
        speak_text(text, voice="Milena", rate=190)


def list_available_voices() -> list[dict[str, str]]:
    """Возвращает список доступных голосов для say."""
    try:
        result = subprocess.run(
            ["say", "-v", "?"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        voices: list[dict[str, str]] = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split(maxsplit=2)
            if len(parts) >= 2:
                name = parts[0]
                lang = parts[1].strip("()")
                desc = parts[2] if len(parts) > 2 else ""
                voices.append({"name": name, "language": lang, "description": desc})
        return voices
    except Exception:
        return []
