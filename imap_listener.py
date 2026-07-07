"""
IMAP IDLE listener — получает уведомления о новых письмах в реальном времени.
Не требует опросов — Gmail сам присылает сигнал о новом письме.

Использует raw socket (не imaplib), потому что imaplib не поддерживает
inline SASL initial response, который требуется Gmail для XOAUTH2.

Включает healthcheck watchdog:
- Отслеживает uptime и количество переподключений
- Периодическая проверка IMAP доступности
- Вызов healthcheck_callback при проблемах
"""

from __future__ import annotations

import base64
import select
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable

from google.auth.transport.requests import Request
from config import Config
from auth import get_gmail_credentials


SSL_PORT = 993
IMAP_HOST = "imap.gmail.com"

# Константы healthcheck
HEALTHCHECK_INTERVAL = 300  # Проверка здоровья каждые 5 минут
TOKEN_REFRESH_INTERVAL = 600  # Обновление токена каждые 10 минут
IDLE_TIMEOUT = 25 * 60  # Таймаут IDLE (25 минут)
MAX_CONSECUTIVE_ERRORS = 5  # Максимум ошибок до срабатывания healthcheck


def _get_access_token() -> str | None:
    """Получает свежий access token для IMAP XOAUTH2."""
    creds = get_gmail_credentials()
    if not creds:
        return None
    creds.refresh(Request())
    return creds.token  # type: ignore[return-value]


def _xoauth2_string(email: str, token: str) -> str:
    """Формирует base64-encoded XOAUTH2 строку для IMAP."""
    auth = f"user={email}\1auth=Bearer {token}\1\1"
    return base64.b64encode(auth.encode()).decode()


def check_imap_connectivity() -> tuple[bool, str]:
    """
    Быстрая проверка доступности IMAP-сервера Gmail.

    Returns:
        (True, "ok") или (False, "причина ошибки").
    """
    try:
        raw = socket.create_connection((IMAP_HOST, SSL_PORT), timeout=10)
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(raw, server_hostname=IMAP_HOST)

        # Читаем greeting
        buf = b""
        while not buf.endswith(b"\r\n"):
            chunk = sock.recv(1)
            if not chunk:
                sock.close()
                return False, "Connection closed on greeting"
            buf += chunk

        greeting = buf.decode("latin-1").strip()
        sock.close()
        if "OK" in greeting:
            return True, "ok"
        return False, f"Unexpected greeting: {greeting[:100]}"
    except socket.timeout:
        return False, "Connection timeout"
    except ConnectionRefusedError:
        return False, "Connection refused"
    except Exception as e:
        return False, str(e)


class _IMAPConnection:
    """Низкоуровневое IMAP-соединение с XOAUTH2 + inline initial response."""

    def __init__(self, email: str, token: str):
        self.email = email
        self.sock: socket.socket | None = None
        self._connect(token)

    def _connect(self, token: str) -> None:
        raw = socket.create_connection((IMAP_HOST, SSL_PORT), timeout=30)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=IMAP_HOST)

        # Читаем greeting
        self._recv_line()

        # XOAUTH2 с inline initial response
        b64 = _xoauth2_string(self.email, token)
        self._send(f"A001 AUTHENTICATE XOAUTH2 {b64}")

        # Ответ: может быть + ! или OK
        resp = self._recv_line()
        if resp.startswith("+"):
            # Сервер попросил ещё данные — отправляем пустую строку
            self._send("")
            resp = self._recv_line()

        if "NO" in resp or "BAD" in resp or not resp:
            raise ConnectionError(f"XOAUTH2 failed: {resp}")

        # Выбираем INBOX
        self._send("A002 SELECT INBOX")
        self._recv_line()  # FLAGS
        self._recv_line()  # OK
        # Дочитываем до конца ответа SELECT
        while True:
            line = self._recv_line()
            if line.startswith("A002 OK") or line.startswith("A002 NO"):
                break

    def _send(self, cmd: str) -> None:
        assert self.sock
        self.sock.sendall((cmd + "\r\n").encode("latin-1"))

    def _recv_line(self) -> str:
        assert self.sock
        buf = b""
        while not buf.endswith(b"\r\n"):
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("Connection closed")
            buf += chunk
        return buf.decode("latin-1").strip()

    def send_idle(self) -> None:
        """Отправляет команду IDLE."""
        self._send("A003 IDLE")

    def recv_tagged(self, tag: str = "A001") -> str:
        """Читает ответ до tagged response."""
        while True:
            line = self._recv_line()
            if line.startswith(f"{tag} ") or line.startswith(f"{tag}*"):
                return line

    def wait_for_data(self, timeout: float = IDLE_TIMEOUT) -> bool:
        """Ждёт данные на сокете. Возвращает True если есть данные."""
        assert self.sock
        ready = select.select([self.sock], [], [], timeout)
        return bool(ready[0])

    def read_line(self) -> str:
        """Читает одну строку."""
        return self._recv_line()

    def send_done(self) -> None:
        """Выходит из IDLE."""
        self._send("DONE")

    def logout(self) -> None:
        try:
            self._send("A004 LOGOUT")
            self._recv_line()
        except Exception:
            pass
        finally:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass


class GmailWatcher:
    """
    Следит за новыми письмами через IMAP IDLE.

    При получении уведомления вызывает on_notification(), которая
    должна обработать новые письма через Gmail API.

    Поддерживает healthcheck_callback для мониторинга состояния соединения.
    """

    def __init__(self, email: str, on_notification: Callable,
                 healthcheck_callback: Callable | None = None):
        self.email = email
        self.on_notification = on_notification
        self.healthcheck_callback = healthcheck_callback
        self._running = False

        # Статистика соединения
        self.stats: dict[str, Any] = {
            "start_time": datetime.now(timezone.utc).isoformat(),
            "reconnects": 0,
            "notifications_received": 0,
            "last_notification_time": "",
            "last_healthcheck_time": "",
            "last_healthcheck_status": "unknown",
            "consecutive_errors": 0,
            "total_idle_cycles": 0,
            "status": "stopped",
        }

    @property
    def uptime_seconds(self) -> float:
        """Секунд с момента запуска."""
        if not self.stats.get("start_time"):
            return 0.0
        start = datetime.fromisoformat(self.stats["start_time"])
        return (datetime.now(timezone.utc) - start).total_seconds()

    @property
    def uptime_str(self) -> str:
        """Человекочитаемый uptime."""
        secs = self.uptime_seconds
        h, m = divmod(int(secs), 3600)
        m, s = divmod(m, 60)
        if h > 0:
            return f"{h}ч {m}мин"
        elif m > 0:
            return f"{m}мин {s}сек"
        return f"{s}сек"

    def run(self):
        """Запускает цикл IMAP IDLE с авто-переподключением и healthcheck."""
        self._running = True
        self.stats["start_time"] = datetime.now(timezone.utc).isoformat()
        self.stats["status"] = "running"
        print(f"🔔 GmailWatcher для {self.email}")
        print("   Режим: IMAP IDLE (реальное время, без опросов)")

        last_healthcheck = 0.0

        while self._running:
            try:
                self._idle_loop()
            except (ConnectionError, OSError, Exception) as e:
                self.stats["reconnects"] += 1
                self.stats["consecutive_errors"] += 1
                print(f"  ⚠️  Разрыв соединения: {e}")
                print("  🔄 Переподключение через 5 сек...")

                # Healthcheck при каждом разрыве
                if (self.healthcheck_callback
                        and self.stats["consecutive_errors"] >= 3):
                    try:
                        self.healthcheck_callback(
                            "warning",
                            f"Разрыв соединения ({self.stats['consecutive_errors']} подряд): {e}",
                        )
                    except Exception:
                        pass

                time.sleep(5)
            except KeyboardInterrupt:
                break

        self.stats["status"] = "stopped"
        print(f"  📊 Статистика: {self.stats['notifications_received']} уведомлений, "
              f"{self.stats['reconnects']} переподключений, "
              f"uptime {self.uptime_str}")

    def stop(self):
        """Останавливает цикл."""
        self._running = False

    def _run_healthcheck(self) -> str:
        """Запускает проверку IMAP-доступности. Возвращает статус."""
        ok, msg = check_imap_connectivity()
        status = "ok" if ok else "error"
        self.stats["last_healthcheck_time"] = datetime.now(timezone.utc).isoformat()
        self.stats["last_healthcheck_status"] = status

        if status != "ok" and self.healthcheck_callback:
            try:
                self.healthcheck_callback(status, msg)
            except Exception:
                pass

        return status

    def _idle_loop(self):
        """Подключается к IMAP и входит в цикл IDLE."""
        print("  🔌 Подключение к imap.gmail.com:993...")
        token = _get_access_token()
        if not token:
            self.stats["consecutive_errors"] += 1
            raise ConnectionError("Не удалось получить access token")

        self.stats["consecutive_errors"] = 0
        imap = _IMAPConnection(self.email, token)
        print("  ✅ Подключено. Ожидание новых писем (IDLE)...\n")

        last_token_refresh = time.monotonic()
        last_healthcheck_time = time.monotonic()

        while self._running:
            # Каждые 10 минут обновляем токен — переподключаемся
            if time.monotonic() - last_token_refresh > TOKEN_REFRESH_INTERVAL:
                print("  🔄 Обновление токена...")
                break

            # Периодический healthcheck
            now = time.monotonic()
            if now - last_healthcheck_time > HEALTHCHECK_INTERVAL:
                hc_status = self._run_healthcheck()
                status_emoji = "✅" if hc_status == "ok" else "⚠️"
                print(f"  {status_emoji} Healthcheck: {hc_status}")
                last_healthcheck_time = now

            # Входим в IDLE
            imap.send_idle()
            line = imap.read_line()  # "+ idling"
            if not line.startswith("+"):
                print(f"  ⚠️  IDLE неожиданный ответ: {line}")
                break

            # Ждём уведомление от сервера (до 25 минут)
            has_data = imap.wait_for_data(IDLE_TIMEOUT)

            # Выходим из IDLE
            imap.send_done()
            self.stats["total_idle_cycles"] += 1

            if has_data:
                # Пришло уведомление — читаем строку уведомления
                notification = imap.read_line()
                print(f"  📩 Сигнал: {notification}")
                self.stats["notifications_received"] += 1
                self.stats["last_notification_time"] = (
                    datetime.now(timezone.utc).isoformat()
                )
                self.on_notification()
            else:
                # Таймаут — keepalive, просто продолжаем
                pass

            # Читаем завершающий OK IDLE completed
            try:
                imap.read_line()
            except Exception:
                pass

        imap.logout()


def run_healthcheck_cmd() -> dict[str, Any]:
    """CLI-команда: проверяет IMAP-доступность и возвращает результат."""
    print("🔍 Проверка IMAP-доступности...")
    ok, msg = check_imap_connectivity()

    if ok:
        print("  ✅ IMAP доступен")
        return {"status": "ok", "detail": msg}
    else:
        print(f"  ❌ IMAP недоступен: {msg}")
        return {"status": "error", "detail": msg}
