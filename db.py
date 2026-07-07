"""
SQLite-база истории решений, коррекций, правил отправителей и статистики.
"""

from __future__ import annotations

import sqlite3
import os
from datetime import datetime, date, timedelta
from typing import Any

DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DB_DIR, "email_sorter.db")


def _ensure_dir() -> None:
    os.makedirs(DB_DIR, exist_ok=True)


def _get_conn() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # быстрее при конкурентном доступе
    return conn


def init_db() -> None:
    """Создаёт таблицы, если их нет."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            subject TEXT DEFAULT '',
            sender TEXT DEFAULT '',
            decision TEXT NOT NULL,
            action TEXT DEFAULT '',
            label TEXT DEFAULT '',
            confidence REAL DEFAULT 0.0,
            reason TEXT DEFAULT '',
            extra_info TEXT DEFAULT '',       -- JSON с деталями (сумма, магазин, PR, ...)
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_processed_at
        ON email_decisions(processed_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_decision
        ON email_decisions(decision)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_message_id
        ON email_decisions(message_id)
    """)

    # Таблица коррекций: пользователь отменил решение
    conn.execute("""
        CREATE TABLE IF NOT EXISTS correction_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            sender TEXT DEFAULT '',
            domain TEXT DEFAULT '',
            original_decision TEXT NOT NULL,
            corrected_decision TEXT NOT NULL,
            source TEXT DEFAULT 'manual',    -- manual | trash_scan
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_correction_domain
        ON correction_feedback(domain)
    """)

    # Таблица персистентных правил для отправителей
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sender_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_pattern TEXT NOT NULL UNIQUE,   -- email или @domain
            decision TEXT NOT NULL,
            hit_count INTEGER DEFAULT 1,
            is_auto INTEGER DEFAULT 1,             -- 1 = авто-создано, 0 = ручное
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sender_rules_pattern
        ON sender_rules(sender_pattern)
    """)

    conn.commit()
    conn.close()


def _migrate_db() -> None:
    """Добавляет новые колонки при обновлении (без потери данных)."""
    conn = _get_conn()
    for col, col_type in [
        ("extra_info", "TEXT DEFAULT ''"),
        ("snippet", "TEXT DEFAULT ''"),
        ("digest_sent", "INTEGER DEFAULT 0"),
        ("urgency", "TEXT DEFAULT 'low'"),
        ("phishing_score", "REAL DEFAULT 0.0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE email_decisions ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
    conn.commit()
    conn.close()


# ─── Решения ───────────────────────────────────────────

def record_decision(
    message_id: str,
    subject: str = "",
    sender: str = "",
    decision: str = "keep",
    action: str = "keep",
    label: str = "",
    confidence: float = 0.0,
    reason: str = "",
    extra_info: str = "",
    urgency: str = "low",
    phishing_score: float = 0.0,
) -> None:
    """Записывает результат обработки одного письма."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO email_decisions
           (message_id, subject, sender, decision, action, label, confidence, reason, extra_info, urgency, phishing_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (message_id, subject[:200], sender[:200], decision, action, label, confidence, reason[:500], extra_info[:1000], urgency, phishing_score),
    )
    conn.commit()
    conn.close()


def already_processed(message_id: str) -> bool:
    """Проверяет, обрабатывалось ли это письмо ранее."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM email_decisions WHERE message_id = ?", (message_id,)
    ).fetchone()
    conn.close()
    return row is not None


def get_decision(message_id: str) -> dict[str, Any] | None:
    """Возвращает запись решения по message_id."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM email_decisions WHERE message_id = ?", (message_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_history(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """Последние N записей."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM email_decisions
           ORDER BY processed_at DESC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_snippet(message_id: str, snippet: str) -> None:
    """Сохраняет сниппет письма (для дайджеста)."""
    conn = _get_conn()
    conn.execute(
        "UPDATE email_decisions SET snippet = ? WHERE message_id = ?",
        (snippet[:500], message_id),
    )
    conn.commit()
    conn.close()


def get_digest_entries(target_date: date | None = None) -> list[dict[str, Any]]:
    """Неотправленные записи для дайджеста (delete/archive со сниппетом)."""
    if target_date is None:
        target_date = date.today()
    conn = _get_conn()
    rows = conn.execute(
        """SELECT message_id, subject, sender, decision, label, snippet, extra_info
           FROM email_decisions
           WHERE date(processed_at) = ?
             AND decision IN ('delete', 'archive')
             AND digest_sent = 0
             AND snippet != ''
           ORDER BY processed_at DESC""",
        (target_date.isoformat(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_digest_sent(message_ids: list[str]) -> None:
    """Отмечает записи как отправленные в дайджесте."""
    if not message_ids:
        return
    conn = _get_conn()
    placeholders = ",".join("?" for _ in message_ids)
    conn.execute(
        f"UPDATE email_decisions SET digest_sent = 1 WHERE message_id IN ({placeholders})",
        message_ids,
    )
    conn.commit()
    conn.close()


def get_history_by_date(target_date: date | None = None) -> list[dict[str, Any]]:
    if target_date is None:
        target_date = date.today()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM email_decisions WHERE date(processed_at) = ? ORDER BY processed_at DESC",
        (target_date.isoformat(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_stats(target_date: date | None = None) -> dict[str, Any]:
    if target_date is None:
        target_date = date.today()
    date_str = target_date.isoformat()

    conn = _get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM email_decisions WHERE date(processed_at) = ?", (date_str,)
    ).fetchone()[0]

    by_decision = conn.execute(
        "SELECT decision, COUNT(*) as cnt FROM email_decisions WHERE date(processed_at) = ? GROUP BY decision",
        (date_str,),
    ).fetchall()

    by_label = conn.execute(
        "SELECT label, COUNT(*) as cnt FROM email_decisions WHERE date(processed_at) = ? AND label != '' GROUP BY label ORDER BY cnt DESC",
        (date_str,),
    ).fetchall()

    low_confidence = conn.execute(
        "SELECT COUNT(*) FROM email_decisions WHERE date(processed_at) = ? AND confidence < 0.7 AND confidence > 0",
        (date_str,),
    ).fetchone()[0]

    conn.close()

    decisions = {r["decision"]: r["cnt"] for r in by_decision}
    labels = {r["label"]: r["cnt"] for r in by_label}

    return {
        "date": date_str,
        "total": total,
        "deleted": decisions.get("delete", 0),
        "archived": decisions.get("archive", 0),
        "kept": decisions.get("keep", 0),
        "reviewed": decisions.get("review", 0),
        "low_confidence": low_confidence,
        "labels": labels,
    }


def get_total_stats() -> dict[str, Any]:
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM email_decisions").fetchone()[0]
    by_decision = conn.execute(
        "SELECT decision, COUNT(*) as cnt FROM email_decisions GROUP BY decision"
    ).fetchall()
    conn.close()
    decisions = {r["decision"]: r["cnt"] for r in by_decision}
    return {
        "total": total,
        "deleted": decisions.get("delete", 0),
        "archived": decisions.get("archive", 0),
        "kept": decisions.get("keep", 0),
        "reviewed": decisions.get("review", 0),
    }


def get_top_senders(limit: int = 20) -> list[dict[str, Any]]:
    """Топ отправителей по количеству писем."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT sender, COUNT(*) as cnt,
                  SUM(CASE WHEN decision='delete' THEN 1 ELSE 0 END) as deleted_cnt
           FROM email_decisions
           WHERE sender != ''
           GROUP BY sender
           ORDER BY cnt DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats_weekly() -> dict[str, Any]:
    """Статистика за последние 7 дней по дням."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT date(processed_at) as day,
                  COUNT(*) as total,
                  SUM(CASE WHEN decision='delete' THEN 1 ELSE 0 END) as deleted
           FROM email_decisions
           WHERE processed_at >= date('now', '-7 days')
           GROUP BY date(processed_at)
           ORDER BY day""",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Коррекции ─────────────────────────────────────────

def record_correction(
    message_id: str,
    sender: str = "",
    original_decision: str = "",
    corrected_decision: str = "",
    source: str = "manual",
) -> None:
    """Записывает коррекцию: пользователь исправил решение."""
    domain = ""
    if sender:
        import re
        m = re.search(r"@([\w.-]+)", sender)
        if m:
            domain = m.group(1).lower()

    conn = _get_conn()
    conn.execute(
        """INSERT INTO correction_feedback
           (message_id, sender, domain, original_decision, corrected_decision, source)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (message_id, sender[:200], domain, original_decision, corrected_decision, source),
    )
    conn.commit()
    conn.close()


def get_corrections_by_domain(limit: int = 50) -> list[dict[str, Any]]:
    """Домены, где чаще всего ошибается классификатор."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT domain, COUNT(*) as cnt,
                  GROUP_CONCAT(DISTINCT original_decision) as wrong_decisions
           FROM correction_feedback
           WHERE domain != ''
           GROUP BY domain
           ORDER BY cnt DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Правила отправителей ──────────────────────────────

def set_sender_rule(sender_pattern: str, decision: str, is_auto: int = 1) -> None:
    """Создаёт или обновляет правило для отправителя."""
    conn = _get_conn()
    existing = conn.execute(
        "SELECT id, hit_count FROM sender_rules WHERE sender_pattern = ?",
        (sender_pattern,),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE sender_rules SET decision=?, hit_count=hit_count+1, updated_at=CURRENT_TIMESTAMP, is_auto=? WHERE id=?",
            (decision, is_auto, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO sender_rules (sender_pattern, decision, hit_count, is_auto) VALUES (?, ?, 1, ?)",
            (sender_pattern, decision, is_auto),
        )
    conn.commit()
    conn.close()


def get_sender_rule(sender: str) -> dict[str, Any] | None:
    """Проверяет, есть ли правило для отправителя (сначала точный email, потом домен)."""
    import re
    domain = ""
    m = re.search(r"@([\w.-]+)", sender)
    if m:
        domain = m.group(1).lower()

    conn = _get_conn()
    # Ищем по точному email, потом по @domain, потом по domain
    for pattern in [sender, f"@{domain}", domain]:
        if not pattern:
            continue
        row = conn.execute(
            "SELECT * FROM sender_rules WHERE sender_pattern = ?", (pattern,)
        ).fetchone()
        if row:
            conn.close()
            return dict(row)
    conn.close()
    return None


def get_sender_rules(limit: int = 50) -> list[dict[str, Any]]:
    """Все правила, отсортированные по популярности."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM sender_rules ORDER BY hit_count DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Probes: задачи, подписки, расходы ─────────────────

def init_probes_tables() -> None:
    """Создаёт таблицы для данных из probes.py."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extracted_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            task TEXT NOT NULL,
            due TEXT DEFAULT '',
            priority TEXT DEFAULT 'medium',
            completed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES email_decisions(message_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tasks_message
        ON extracted_tasks(message_id)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            service TEXT DEFAULT '',
            amount TEXT DEFAULT '',
            period TEXT DEFAULT '',
            next_billing TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES email_decisions(message_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_subscriptions_service
        ON subscriptions(service)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            amount TEXT DEFAULT '',
            currency TEXT DEFAULT 'RUB',
            merchant TEXT DEFAULT '',
            category TEXT DEFAULT 'other',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES email_decisions(message_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_expenses_category
        ON expenses(category)
    """)
    conn.commit()
    conn.close()


def save_task(message_id: str, task: str, due: str = "",
              priority: str = "medium") -> None:
    """Сохраняет извлечённую задачу."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO extracted_tasks
           (message_id, task, due, priority)
           VALUES (?, ?, ?, ?)""",
        (message_id, task[:300], due[:100], priority),
    )
    conn.commit()
    conn.close()


def save_subscription(message_id: str, service: str, amount: str = "",
                      period: str = "monthly", next_billing: str = "") -> None:
    """Сохраняет обнаруженную подписку."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO subscriptions
           (message_id, service, amount, period, next_billing)
           VALUES (?, ?, ?, ?, ?)""",
        (message_id, service[:200], amount[:50], period[:50], next_billing[:100]),
    )
    conn.commit()
    conn.close()


def save_expense(message_id: str, amount: str, currency: str = "RUB",
                 merchant: str = "", category: str = "other") -> None:
    """Сохраняет обнаруженный расход."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO expenses
           (message_id, amount, currency, merchant, category)
           VALUES (?, ?, ?, ?, ?)""",
        (message_id, amount[:50], currency[:10], merchant[:200], category[:50]),
    )
    conn.commit()
    conn.close()


def get_today_tasks() -> list[dict[str, Any]]:
    """Задачи, извлечённые сегодня."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM extracted_tasks
           WHERE date(created_at) = date('now')
           ORDER BY
             CASE priority
               WHEN 'high' THEN 0
               WHEN 'medium' THEN 1
               ELSE 2
             END,
             created_at DESC""",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_subscriptions() -> list[dict[str, Any]]:
    """Активные подписки."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE active = 1 ORDER BY created_at DESC",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_today_expenses() -> list[dict[str, Any]]:
    """Расходы за сегодня."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM expenses
           WHERE date(created_at) = date('now')
           ORDER BY created_at DESC""",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Инициализация ────────────────────────────────────
init_db()
_migrate_db()
init_probes_tables()
