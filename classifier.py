"""
Классификация писем через локальную LLM (Ollama).
Определяет: важное (keep), неважное (delete/archive), требует проверки (review).

Поддерживает:
- Белый/чёрный список доменов (без вызова LLM)
- Метка типа письма (newsletter, social, receipt, work, personal, ...)
- Уверенность классификации (confidence)
- Кэш решений по домену отправителя
- Сервис-специфичные плагины (GitHub → PR#, банк → сумма, и т.д.)
- extra_info — детали письма (сумма, магазин, PR, событие, ...)
"""

from __future__ import annotations

import json
import re
import requests

from config import Config

# ─── Сервис-специфичные плагины ──────────────────────────
# Каждый плагин — это словарь с:
#   "domains": список доменов, к которым применяется
#   "hint":   дополнительная инструкция в промпт
#   "extract_fields": какие поля ожидать в extra_info

SERVICE_PLUGINS: list[dict] = [
    {
        "domains": ["github.com", "github"],
        "hint": (
            "If this is a GitHub notification, extract PR/issue number, repo name, and action "
            "(opened/merged/reviewed/closed/commented). "
        ),
        "extract_fields": ["pr_number", "repo", "action", "reviewer"],
    },
    {
        "domains": ["trello.com", "asana.com", "notion.so", "miro.com", "figma.com", "linear.app"],
        "hint": (
            "If this is a project management notification (Trello/Asana/Notion/etc.), "
            "extract the task/card name, board/project name, and what action was taken. "
        ),
        "extract_fields": ["task_name", "project", "action", "assignee"],
    },
    {
        "domains": ["sberbank.ru", "alfabank.ru", "tinkoff.ru", "vtb.ru", "raiffeisen.ru",
                      "bank", "monobank.ua", "privatbank.ua"],
        "hint": (
            "If this is a bank notification about a transaction, extract the amount, "
            "currency, merchant name, and whether it's a debit or credit. "
        ),
        "extract_fields": ["amount", "currency", "merchant", "transaction_type", "balance"],
    },
    {
        "domains": ["zoom.us", "meet.google.com", "teams.microsoft.com", "gotomeeting.com",
                      "webex.com", "calendly.com", "calendly"],
        "hint": (
            "If this is a meeting invitation or reminder, extract the event title, "
            "date/time, and meeting link. "
        ),
        "extract_fields": ["event_title", "event_time", "meeting_link", "organizer"],
    },
    {
        "domains": ["aliexpress.com", "aliexpress.ru", "ozon.ru", "wb.ru", "wildberries.ru",
                      "amazon.com", "ebay.com", "etsy.com", "shein.com", "yandex.market",
                      "market.yandex.ru", "avito.ru"],
        "hint": (
            "If this is an order/delivery notification, extract the order number, "
            "store name, item name, amount, and delivery status. "
        ),
        "extract_fields": ["order_number", "store", "item", "amount", "status"],
    },
    {
        "domains": ["linkedin.com", "linkedin", "facebook.com", "twitter.com", "x.com",
                      "instagram.com", "reddit.com", "medium.com", "habr.com", "pikabu.ru"],
        "hint": (
            "If this is a social media notification, extract the platform, "
            "action (like/comment/follow/share), and who performed it. "
        ),
        "extract_fields": ["platform", "action", "user", "post_title"],
    },
    {
        "domains": ["atlassian.com", "jira", "bitbucket.org", "gitlab.com", "gitlab",
                      "jetbrains.com", "codefresh.io", "circleci.com", "travis-ci.com",
                      "jenkins.io"],
        "hint": (
            "If this is a CI/CD, Jira, or dev tool notification, extract the project name, "
            "issue/ticket number, status change, and build/run result. "
        ),
        "extract_fields": ["project", "ticket_number", "status", "build_result"],
    },
    {
        "domains": ["docker.com", "dockerhub", "pypi.org", "npmjs.com", "rubygems.org",
                      "packagist.org", "crates.io", "nuget.org"],
        "hint": (
            "If this is a package registry notification, extract the package name, "
            "version, and action (published/updated/deprecated). "
        ),
        "extract_fields": ["package_name", "version", "action"],
    },
]

SYSTEM_PROMPT = """You are an email sorting assistant. Your task is to read an email and decide what to do with it.

Possible decisions:
- "keep" — important, needs attention (personal correspondence, work, orders, bank, urgent)
- "archive" — not urgent but might be useful later (notifications, digests, automated messages)
- "delete" — spam, ads, marketing, newsletters the user doesn't want

Possible labels (email type):
- "personal" — from friends, family, personal contacts
- "work" — work-related, colleagues, projects
- "receipt" — orders, deliveries, payments, invoices
- "notification" — automated service notifications
- "social" — social media notifications (LinkedIn, Twitter, Facebook, Instagram)
- "marketing" — ads, promotions, newsletters, marketing emails
- "newsletter" — subscription newsletters, digests
- "spam" — obvious spam or phishing
- "other" — anything that doesn't fit above

Confidence: how sure you are about the decision (0.0 = not sure, 1.0 = absolutely sure).

Urgency: how urgently the user needs to respond or act on this email.
- "high" — must reply/act today (urgent work issue, deadline today, bank alert, meeting starting soon)
- "medium" — needs attention in 1-2 days (meeting tomorrow, pending task, non-critical update)
- "low" — can wait, informational only (digest, newsletter, notification, receipt)

Phishing: rate how likely this email is a phishing attempt (0.0 = legitimate, 1.0 = definitely phishing).
Look for: suspicious links, urgency/pressure language, mismatched sender domain, requests for password/payment info, poor grammar from official-looking sender.

{additional_hints}

Respond ONLY with a JSON object in this exact format (no other text):
{"decision": "keep" | "delete" | "archive", "reason": "short explanation in Russian", "label": "personal | work | receipt | notification | social | marketing | newsletter | spam | other", "confidence": 0.95, "urgency": "low" | "medium" | "high", "phishing_score": 0.0, "extra_info": {}}

The "extra_info" field should contain any relevant details extracted from the email (leave empty object if nothing specific). Do not include any other text in your response."""


def _get_plugin_for_domain(domain: str) -> dict | None:
    """Возвращает плагин для домена отправителя, если есть."""
    domain_lower = domain.lower()
    for plugin in SERVICE_PLUGINS:
        for d in plugin["domains"]:
            if domain_lower == d or domain_lower.endswith("." + d):
                return plugin
    return None


def _build_prompt(email_text: str, sender: str = "") -> str:
    """Строит промпт с учётом сервис-специфичного плагина."""
    additional_hints = ""

    if sender:
        domain_match = re.search(r"@([\w.-]+)", sender)
        if domain_match:
            plugin = _get_plugin_for_domain(domain_match.group(1))
            if plugin:
                hint = plugin["hint"]
                fields = ", ".join(plugin["extract_fields"])
                additional_hints = (
                    f"Sender domain: {domain_match.group(1)}\n"
                    f"{hint}\n"
                    f"Include these fields in extra_info if applicable: {fields}\n"
                )

    prompt_text = SYSTEM_PROMPT.replace("{additional_hints}", additional_hints)
    return f"{prompt_text}\n\nEmail:\n{email_text[:4000]}"


def _check_domain_lists(sender: str) -> dict | None:
    """Проверяет домен отправителя в белом/чёрном списке.
    Возвращает решение без вызова LLM, или None если домен не найден."""
    if not sender:
        return None
    domain_match = re.search(r"@([\w.-]+)", sender)
    if not domain_match:
        return None
    domain = domain_match.group(1).lower()

    # Чёрный список — удаляем сразу
    for d in Config.AUTO_DELETE_DOMAINS:
        if domain == d or domain.endswith("." + d):
            return {
                "decision": "delete",
                "reason": f"Домен {domain} в чёрном списке",
                "label": "marketing",
                "confidence": 1.0,
                "urgency": "low",
                "phishing_score": 0.0,
                "extra_info": {},
            }

    # Белый список — сохраняем сразу
    for d in Config.AUTO_KEEP_DOMAINS:
        if domain == d or domain.endswith("." + d):
            return {
                "decision": "keep",
                "reason": f"Домен {domain} в белом списке",
                "label": "work",
                "confidence": 1.0,
                "urgency": "medium",
                "phishing_score": 0.0,
                "extra_info": {},
            }

    return None


def _extract_decision(raw: str) -> dict | None:
    """Извлекает решение из текста, который может быть кривым JSON."""
    dec_match = re.search(r'"(?:decision)"\s*:\s*"(keep|delete|archive)"', raw)
    if not dec_match:
        return None
    decision = dec_match.group(1)

    reason = ""
    rea_match = re.search(r'"reason"\s*:\s*"(.+)"\s*[,}]', raw, re.DOTALL)
    if rea_match:
        reason = rea_match.group(1).strip()
        reason = re.sub(r'^"', "", reason)
        reason = reason.replace('""', "")

    label = ""
    label_match = re.search(r'"label"\s*:\s*"(\w+)"', raw)
    if label_match:
        label = label_match.group(1)

    confidence = 0.0
    conf_match = re.search(r'"confidence"\s*:\s*([\d.]+)', raw)
    if conf_match:
        try:
            confidence = float(conf_match.group(1))
        except ValueError:
            confidence = 0.0

    # Извлекаем urgency
    urgency = "low"
    urg_match = re.search(r'"urgency"\s*:\s*"(low|medium|high)"', raw)
    if urg_match:
        urgency = urg_match.group(1)

    # Извлекаем phishing_score
    phishing_score = 0.0
    ph_match = re.search(r'"phishing_score"\s*:\s*([\d.]+)', raw)
    if ph_match:
        try:
            phishing_score = min(1.0, max(0.0, float(ph_match.group(1))))
        except ValueError:
            phishing_score = 0.0

    # Парсим extra_info — ищем JSON-объект после "extra_info":
    extra_info: dict = {}
    ei_match = re.search(r'"extra_info"\s*:\s*(\{[^}]*\})', raw, re.DOTALL)
    if ei_match:
        try:
            extra_info = json.loads(ei_match.group(1))
        except (json.JSONDecodeError, ValueError):
            extra_info = {}

    return {
        "decision": decision,
        "reason": reason or "без объяснения",
        "label": label,
        "confidence": confidence,
        "urgency": urgency,
        "phishing_score": phishing_score,
        "extra_info": extra_info,
    }


# Кэш решений по домену отправителя
_decision_cache: dict[str, dict] = {}


def classify_email(email_text: str, sender: str = "") -> dict:
    """
    Отправляет текст письма в локальную модель Ollama и возвращает решение.

    Алгоритм:
    1. Проверяет белый/чёрный список доменов → если найден, возвращает без LLM
    2. Проверяет кэш по домену → если есть, возвращает кэшированное
    3. Определяет сервис-специфичный плагин по домену → дополняет промпт
    4. Вызывает Ollama
    5. Парсит JSON-ответ (decision + label + confidence + reason + extra_info)
    6. Кэширует решение по домену отправителя

    Возвращает:
    {"decision": "keep" | "delete" | "archive", "reason": "...", "label": "...",
     "confidence": 0.95, "extra_info": {...}}
    """
    # 1. Проверка белого/чёрного списка
    domain_check = _check_domain_lists(sender)
    if domain_check:
        return domain_check

    # 2. Проверка кэша по домену
    if sender:
        domain_match = re.search(r"@([\w.-]+)", sender)
        if domain_match:
            domain = domain_match.group(1).lower()
            if domain in _decision_cache:
                cached = _decision_cache[domain]
                return {
                    "decision": cached["decision"],
                    "reason": f"{cached['reason']} (кэш)",
                    "label": cached.get("label", ""),
                    "confidence": cached.get("confidence", 0.0),
                    "urgency": cached.get("urgency", "low"),
                    "phishing_score": cached.get("phishing_score", 0.0),
                    "extra_info": cached.get("extra_info", {}),
                }

    # 3. Строим промпт с учётом плагина
    prompt = _build_prompt(email_text, sender=sender)

    # 4. Вызов Ollama
    try:
        response = requests.post(
            f"{Config.OLLAMA_URL}/api/generate",
            json={
                "model": Config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "temperature": 0.1,
                "max_tokens": 300,
            },
            timeout=30,
        )
        response.raise_for_status()
        raw = response.json()["response"].strip()
    except requests.exceptions.Timeout:
        return {
            "decision": "keep",
            "reason": "Таймаут при вызове LLM, письмо оставлено",
            "label": "other",
            "confidence": 0.0,
            "urgency": "low",
            "phishing_score": 0.0,
            "extra_info": {},
        }
    except requests.exceptions.ConnectionError:
        return {
            "decision": "keep",
            "reason": "Ollama недоступна, письмо оставлено",
            "label": "other",
            "confidence": 0.0,
            "urgency": "low",
            "phishing_score": 0.0,
            "extra_info": {},
        }
    except Exception as e:
        return {
            "decision": "keep",
            "reason": f"Ошибка LLM: {e}",
            "label": "other",
            "confidence": 0.0,
            "urgency": "low",
            "phishing_score": 0.0,
            "extra_info": {},
        }

    parsed = _extract_decision(raw)
    if parsed and parsed.get("decision") in ("keep", "delete", "archive"):
        # Кэшируем по домену
        if sender:
            domain_match = re.search(r"@([\w.-]+)", sender)
            if domain_match:
                _decision_cache[domain_match.group(1).lower()] = parsed
        return parsed

    # fallback — если не удалось распарсить
    return {
        "decision": "keep",
        "reason": f"Ошибка парсинга ответа модели: {raw[:150]}",
        "label": "other",
        "confidence": 0.0,
        "urgency": "low",
        "phishing_score": 0.0,
        "extra_info": {},
    }
