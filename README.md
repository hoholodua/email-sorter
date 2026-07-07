# Gmail Sorter — Умная сортировка почты 🤖

Агент читает непрочитанные письма, через Claude API определяет важность и автоматически удаляет неважные (реклама, рассылки, спам).

## Быстрый старт

### 1. Установка зависимостей

```bash
cd email-sorter
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Настройка Gmail API

1. Открой [Google Cloud Console](https://console.cloud.google.com/)
2. Создай новый проект (или выбери существующий)
3. Перейди в **APIs & Services → Library**, найди **Gmail API** и включи
4. Перейди в **APIs & Services → Credentials**
5. Нажми **Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: `Gmail Sorter`
6. Скачай JSON-файл
7. Положи его в `credentials/gmail_oauth.json`

### 3. Claude API ключ

```bash
cp .env.example .env
# Открой .env и вставь свой ANTHROPIC_API_KEY
```

### 4. Запуск

```bash
# Первый запуск — откроется браузер, войди в Google
python sorter.py

# Постоянный режим (проверка каждые 5 минут)
python sorter.py --watch
```

### 5. Автозапуск (macOS)

```bash
# Установить как launchd-сервис
cp com.gmailsorter.plist.example ~/Library/LaunchAgents/com.gmailsorter.plist
# Отредактировать пути в plist-файле
launchctl load ~/Library/LaunchAgents/com.gmailsorter.plist
```

## Как это работает

```
Gmail (непрочитанные)
    → Python-агент
        → Claude API (классификация)
            → важное → метка "Важное" + "Звёздочка"
            → неважное → корзина
```

## Логика классификации

Claude получает содержимое письма и решает:

| Важное (keep) | Неважное (delete) |
|---|---|
| Личная переписка | Массовые рассылки |
| Письма от коллег/начальства | Реклама |
| Заказы, платежи, доставка | Соцсети (LinkedIn, FB) |
| Банки, госорганы | Маркетинг |
| Требует ответа | Спам/фишинг |

## Безопасность

- Письма отправляются в Claude API для анализа
- Firebase-токен хранится локально в `credentials/token.json`
- Используется OAuth 2.0, приложение имеет права только на модификацию (чтение + удаление)
- Удалённые письма попадают в корзину Gmail (восстанавливаются в течение 30 дней)
