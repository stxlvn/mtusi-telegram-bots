# MTUCI Telegram Schedule Bot 📅

Каждое утро бот заходит в личный кабинет МТУСИ (lk.mtuci.ru), забирает расписание на сегодня и постит его в Telegram-группу: отдельное сообщение по каждому предмету в свой автоматически создаваемый топик, плюс одно закреплённое сообщение-таблица (нативная таблица Telegram) со всем днём целиком. Если пар нет — бот молчит.

## Credits 🙏

Слой авторизации и парсинга расписания (Playwright-логин и парсинг lk.mtuci.ru) взят из проекта [**TheFoxKD/CalendarMTUSI**](https://github.com/TheFoxKD/CalendarMTUSI) (MIT License) — изначально это была синхронизация с Google Calendar. Этот форк оставляет тот же скрапер (с парой багфиксов, см. ниже) и заменяет вывод в Google Calendar на постинг в Telegram.

## Что умеет 🚀

- Логинится в lk.mtuci.ru и парсит сегодняшние пары (предмет, время, преподаватель, аудитория)
- При первом упоминании предмета создаёт под него отдельный топик в форуме группы и постит туда пары этого дня
- Постит единое расписание дня **настоящей нативной таблицей Telegram** (через `sendRichMessage` из Bot API) в чат/топик объявлений, закрепляет его и открепляет вчерашнее
- Если пар нет — ничего не постит
- При сбое логина делает до 3 повторных попыток с паузой, чтобы пережить временные глюки lk.mtuci.ru
- При ошибке: в группу уходит короткое дружелюбное уведомление, а в отдельный приватный чат (опционально) — полный traceback для отладки

## Отличия от upstream

- Синхронизация с Google Calendar (`src/my_calendar/`, код остался в репозитории, но не используется) заменена на `telegram_post.py`
- Исправлен `_get_current_date()` в `schedule_scraper.py`: заголовок на сайте сменился со словесной даты (`"13 ноября 2024"`) на числовую (`"07.08.2026"`), из-за чего оригинальный парсер падал
- Исправлено заполнение/сабмит формы логина в `auth.py`: раньше код брал ссылку на элемент и кликал/печатал в него, что могло сломаться, если форма Keycloak успевала перерендериться; заменено на `page.fill()`/`page.click()`, которые сами находят актуальный элемент перед действием
- Скриншот каждой пары для отладки (`lesson_debug_*.png`) теперь опционален через `SCRAPING_DEBUG_SCREENSHOTS`, а не включён всегда
- Настраиваемый уровень логирования (`LOG_LEVEL`, по умолчанию `INFO`) — в upstream логирование не фильтровалось по уровню, и в stdout сыпался сырой HTML каждой распарсенной пары

## Установка 🛠️

```bash
git clone https://github.com/stxlvn/mtusi-schedule-bot.git
cd mtusi-schedule-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium

cp .env.example .env
# заполните .env: логин/пароль МТУСИ, GROUP_LABEL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

Вся конфигурация — через переменные окружения, полный список см. в [`.env.example`](.env.example). `.env` подхватывается автоматически из папки скрипта, поэтому запуск руками, через cron или через systemd работает одинаково.

## Запуск

Разовый запуск (рассчитан на ежедневный вызов, например через cron):

```bash
python telegram_post.py
```

Пример строки в crontab (в 8:00 по времени сервера):

```
0 8 * * * cd /path/to/mtusi-schedule-bot && ./venv/bin/python telegram_post.py >> log.txt 2>&1
```

## Как получить `TELEGRAM_CHAT_ID`

Добавьте бота в свою группу администратором (нужны права на управление топиками и закрепление сообщений), отправьте любое сообщение, затем посмотрите `id` чата (отрицательное число для супергрупп) через `https://api.telegram.org/bot<TOKEN>/getUpdates`.

## Структура 📁

```
.
├── telegram_post.py     # точка входа — постинг в Telegram (код этого форка)
├── src/
│   ├── core/             # логирование, исключения (из upstream)
│   ├── models/            # ScheduleEvent / Location / LessonType (из upstream)
│   ├── my_calendar/       # неиспользуемая синхронизация с Google Calendar (из upstream)
│   └── scraping/          # логин + парсинг расписания МТУСИ (из upstream, с патчами)
├── tests/
├── .env.example
└── requirements.txt
```

## Разработка

```bash
pip install -r requirements-dev.txt
pre-commit install
pytest
ruff check src
```

## Лицензия 📄

[MIT License](LICENSE), унаследована от upstream-проекта.
