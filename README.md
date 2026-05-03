# MISIS Schedule Bot — полная документация

## 1. Что это за проект

`misis-rasp-bot` — асинхронный Python-проект, который объединяет:

- Telegram-бота (aiogram)
- VK-бота (vkbottle)
- общий парсер расписания с сайта колледжа
- SQLite-хранилище пользователей, расписания, изменений и ДЗ
- фоновый планировщик для авто-проверки изменений расписания

Проект поддерживает:

- выдачу расписания (сегодня / завтра / через 2 дня)
- поиск расписания (группа, преподаватель, аудитория)
- хранение и просмотр домашнего задания
- роль редактора ДЗ
- админ-функции (статус, перепарс, сохранение эталона, управление редакторами, удаление ДЗ)
- уведомления о смене расписания и новом ДЗ

---

## 2. Технологический стек

- Python 3.12+
- `aiogram` — Telegram
- `vkbottle` — VK
- `httpx` + `beautifulsoup4` — HTTP и HTML-парсинг
- `aiosqlite` — асинхронная SQLite
- `aio-pika` — работа с RabbitMQ
- `APScheduler` — фоновые Cron-задачи
- `RabbitMQ` — очередь уведомлений между producer и consumer
- `python-dotenv` — переменные окружения

---

## 3. Структура проекта

- `src/main.py` — точка входа, сборка зависимостей, запуск ботов и джоб
- `src/config.py` — загрузка настроек из `.env`
- `src/db.py` — все SQL-операции
- `src/models.py` — dataclass-модели
- `src/parser.py` — парсер расписания
- `src/group_catalog.py` — каталог групп и соответствий group -> schedule_id
- `src/schedule_search.py` — поиск по группам/преподавателям/аудиториям
- `src/schedule_service.py` — форматирование расписания и сравнение слепков
- `src/scheduler.py` — регулярные фоновые задачи
- `src/notifier.py` — producer/dispatcher уведомлений в Telegram/VK
- `src/message_broker.py` — RabbitMQ broker, очередь и consumer доставки
- `src/lesson_counters.py` — счетчики пройденных пар по группам
- `src/telegram_bot.py` — вся Telegram-логика
- `src/vk_bot.py` — вся VK-логика

---

## 4. Быстрый запуск

1. Скопировать `.env.example` -> `.env`
1. Заполнить токены
1. Установить `uv` (если еще не установлен):

```bash
pip install --upgrade uv
```

1. Установить зависимости:

```bash
uv sync --frozen
```

1. Запустить:

```bash
uv run --frozen -m src.main
```

---

## 5. Docker / VPS

Проект запускается через `docker-compose.yml`.

Миграции БД:

- Используется `Alembic`.
- При старте приложения миграции применяются автоматически до `head`.
- Для уже существующей БД без `alembic_version` выполняется безопасный `stamp head`, чтобы не ломать рабочую схему на VPS.

- В compose поднимаются два сервиса:
  - `bot` — основное приложение
  - `rabbitmq` — брокер очередей для уведомлений
- База и вложения сохраняются в `./runtime` на хосте.
- В контейнер пробрасываются:
  - `DATABASE_PATH=/app/runtime/bot.db`
  - `ATTACHMENTS_PATH=/app/runtime/attachments`
  - `TZ=${APP_TIMEZONE}`
- RabbitMQ поднимается в отдельном контейнере:
  - AMQP: `5672`
  - management UI: `15672`
- Внутри сети Docker бот подключается к RabbitMQ по имени сервиса `rabbitmq`.

Запуск:

```bash
docker compose up -d --build
```

Практический порядок действий на VPS:

1. Обновить код:

```bash
git pull
```

1. Проверить `.env` и убедиться, что там есть блок RabbitMQ:

```env
RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/
RABBITMQ_QUEUE=misis_notifications
RABBITMQ_PREFETCH_COUNT=20
```

1. Пересобрать и перезапустить сервисы:

```bash
docker compose up -d --build
```

Проверка контейнеров:

```bash
docker compose ps
docker compose logs -f bot
docker compose logs -f rabbitmq
```

Проверка миграций в контейнере:

```bash
docker compose exec bot uv run --frozen alembic current
docker compose exec bot uv run --frozen alembic heads
```

Что важно:

- если `RABBITMQ_URL` задан, уведомления идут через очередь
- если `RABBITMQ_URL` пустой, `Broadcaster` автоматически откатывается на прямую отправку без брокера
- для VPS в Docker рекомендован именно режим с RabbitMQ, потому что он мягче обрабатывает всплески уведомлений и не бьет сразу по API Telegram/VK

---

## 6. Конфигурация (`.env`)

- `APP_TIMEZONE` — таймзона APScheduler и контейнера
- `SCHEDULE_URL` — базовый URL расписания (обычно `http://asu.sf-misis.ru/rasp/600`)
- `DATABASE_PATH` — путь к sqlite
- `ATTACHMENTS_PATH` — корень файлов вложений
- `ADMIN_TELEGRAM_ID` — Telegram ID администратора
- `ADMIN_VK_ID` — VK ID администратора
- `TELEGRAM_BOT_TOKEN` — токен Telegram
- `VK_BOT_TOKEN` — токен VK
- `VK_DISABLE_SSL_VERIFY` — отключение SSL-валидации для VK HTTP-клиента
- `RABBITMQ_URL` — адрес подключения к RabbitMQ
- `RABBITMQ_QUEUE` — имя очереди уведомлений
- `RABBITMQ_PREFETCH_COUNT` — сколько сообщений consumer берет в работу одновременно
- `LESSON_COUNTERS_ENABLED` — глобальный выключатель счетчиков пройденных пар
- `LESSON_COUNTERS_PATH` — путь к JSON со счетчиками по группам
- `LESSON_COUNTERS_QUEUE` — отдельная RabbitMQ-очередь задач подсчета пар
- `WEB_CONFIG_SECRET` — секрет подписи cookie-сессий веб-конфигуратора
- `WEB_SUPERUSER_LOGIN` — логин суперпользователя веб-конфигуратора
- `WEB_SUPERUSER_PASSWORD` — пароль суперпользователя веб-конфигуратора
- `WEB_USERS_PATH` — JSON-хранилище веб-пользователей и прав

Рекомендуемый блок для Docker/VPS:

```env
APP_TIMEZONE=Europe/Moscow
SCHEDULE_REQUEST_DELAY_SECONDS=10
SCHEDULE_REQUEST_JITTER_SECONDS=8
SCHEDULE_URL=http://asu.sf-misis.ru/rasp/600
DATABASE_PATH=/app/runtime/bot.db
ATTACHMENTS_PATH=/app/runtime/attachments
ADMIN_TELEGRAM_ID=...
ADMIN_VK_ID=...
TELEGRAM_BOT_TOKEN=...
VK_BOT_TOKEN=...
VK_DISABLE_SSL_VERIFY=false
RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/
RABBITMQ_QUEUE=misis_notifications
RABBITMQ_PREFETCH_COUNT=20
LESSON_COUNTERS_ENABLED=false
LESSON_COUNTERS_PATH=/app/runtime/lesson_counters.json
LESSON_COUNTERS_QUEUE=misis_lesson_counters
WEB_CONFIG_SECRET=change-me-long-random-secret
WEB_SUPERUSER_LOGIN=admin
WEB_SUPERUSER_PASSWORD=change-me
WEB_USERS_PATH=/app/runtime/web_users.json
```

Пояснение по RabbitMQ-переменным:

- `RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/`
  - `guest:guest` — логин и пароль
  - `rabbitmq` — имя сервиса в `docker-compose`
  - `5672` — стандартный AMQP-порт
- `RABBITMQ_QUEUE=misis_notifications`
  - имя очереди, куда producer складывает уведомления и откуда consumer их читает
- `RABBITMQ_PREFETCH_COUNT=20`
  - ограничение на число сообщений, одновременно взятых consumer в обработку
  - меньше — мягче и медленнее
  - больше — быстрее, но агрессивнее
- `LESSON_COUNTERS_QUEUE=misis_lesson_counters`
  - durable-очередь задач подсчета пар по группам
  - используется только счетчиками, уведомления остаются в `RABBITMQ_QUEUE`

Пример `lesson_counters.json`:

```json
{
  "groups": [
    {
      "schedule_id": 600,
      "group_name": "ИСП-25-1",
      "subjects": [
        {
          "subject": "Литература",
          "teacher": "Волошина Н. В.",
          "passed": 10,
          "total": 62
        }
      ]
    }
  ]
}
```

---

## 7. Веб-конфигуратор

Веб-модуль лежит отдельно в `web_configurator/` и запускается независимо от бота:

```bash
uv run uvicorn web_configurator.app:app --host 0.0.0.0 --port 8080
```

В Docker Compose обычный запуск поднимает все сервисы: `bot`, `rabbitmq` и `web`.

```bash
docker compose up -d --build
```

Если вебка не нужна или не поднялась, основной сервис `bot` продолжает работать отдельно.

RabbitMQ поднимается рядом с ботом. Если брокер временно недоступен, бот не падает: RabbitMQ-consumer не стартует, а отправка продолжает работать через прямой fallback.

Возможности:

- метрики аптайма, пользователей TG/VK, сервисов, парсинга, изменений расписания, активных групп и доставки сообщений
- список пользователей с фильтрами: TG/VK, преподаватели/группы, новые/старые
- редактор `lesson_counters.json` с валидацией дисциплин по расписанию группы
- управление веб-пользователями и правами по разделам

Суперпользователь задается через `WEB_SUPERUSER_LOGIN` и `WEB_SUPERUSER_PASSWORD`.

### 7.1. nginx and public dashboard access

Now `docker compose up -d --build` starts `bot`, `rabbitmq`, `web`, and `nginx`.

Flow:

- `web` stays inside the Docker network on `web:8080`
- `nginx` accepts external traffic and proxies it to `web`
- HTTP is exposed on `80`
- HTTPS is exposed on `2443`

Public URL after deploy:

```text
http://dashboard.nekoty.ru
https://dashboard.nekoty.ru:2443
```

Relevant `.env` values:

```env
NGINX_SERVER_NAME=dashboard.nekoty.ru
NGINX_HTTP_PORT=80
NGINX_HTTPS_PORT=2443
WEB_UPSTREAM=web:8080
NGINX_SSL_CERT_PATH=/etc/letsencrypt/live/dashboard.nekoty.ru/fullchain.pem
NGINX_SSL_KEY_PATH=/etc/letsencrypt/live/dashboard.nekoty.ru/privkey.pem
```

DNS setup:

- use `CNAME` if `dashboard.nekoty.ru` should point to another hostname
- use an `A` record if the subdomain should point directly to the VPS IP

Certificate note:

- put certbot certificates into `./deploy/certs`, they are mounted into the container as `/etc/letsencrypt`
- after certificate renewal, restart nginx: `docker compose restart nginx`

Post-deploy check:

```bash
docker compose ps
docker compose logs -f nginx
docker compose logs -f web
```


---

## 7. Как работает парсинг расписания (подробно)

### 7.1. Источники данных

1. Основная страница расписания группы: `/rasp/{schedule_id}`
2. Каталог групп: `/` + `/group/{department_id}`
3. Каталог преподавателей: `/prep`
4. Каталог аудиторий: `/aud`

### 7.2. Класс `ScheduleParser`

Пайплайн:

1. `parse(schedule_id)` -> `fetch_html` -> `_get_with_retry`
2. HTML разбирается `BeautifulSoup`
3. Из `div#titleF` берется имя группы
4. Для каждого `div.titleDate` ищется следующий `div.rasp`
5. Из таблицы дня читаются строки пар (номер, предмет, преподаватель, аудитория)
6. Дата переводится в ISO (`_date_label_to_iso`)
7. Формируется `ScheduleSnapshot`
8. Вычисляется hash (`compute_hash`) по нормализованному содержимому

### 7.3. Ретраи и отказоустойчивость

В `_get_with_retry`:

- несколько попыток (`request_retries`)
- `response.raise_for_status()`
- при ошибке — лог и `asyncio.sleep(backoff * attempt)`
- после исчерпания попыток бросается последняя HTTP-ошибка

### 7.4. Нормализация и хеш

`compute_hash(snapshot)` собирает строковый payload:

- имя группы
- даты (`date_iso`)
- пары, отсортированные по номеру

Итог: `sha256(payload)`.

Это дает стабильный контроль изменений структуры расписания.

### 7.5. Сравнение расписания

`ScheduleComparator.compare(previous, current)`:

- сравнивает baseline и current
- ограничивает проверку ближайшими днями
- для изменившихся дней формирует:
  - `message` (обычный текст)
  - `telegram_message` (HTML)
  - `vk_message`
  - `payload`

Результат (`ChangeSummary`) используется для записи события и массовой рассылки.

---

## 8. Каталоги групп и поиск

### 8.1. `GroupCatalog`

- Загружает все отделения и группы с сайта
- Привязывает `group_name -> schedule_id`
- Поддерживает поиск:
  - по нормализованному имени группы
  - по `schedule_id`

Нормализация: `casefold`, `ё -> е`, выравнивание разных типов дефиса, схлопывание пробелов.

### 8.2. `ScheduleSearchCatalog`

Поиск в порядке:

1. Группы (`GroupCatalog.find_group`)
2. Преподаватели (страница `/prep`)
3. Аудитории (страница `/aud`)

Поддерживает:

- точное совпадение
- частичное совпадение (`_find_partial`):
  - совпадение по слову
  - по префиксу
  - по вхождению

Возвращает `SearchTarget(kind, title, url)`.

---

## 9. Фоновая синхронизация и уведомления

### 9.1. `ScheduleJobs`

Планировщик создает 3 задачи:

1. `save_daily_baseline` — ежедневно в 00:00
2. `save_daily_baseline_fallback` — fallback в 05:00
3. `sync_current_snapshot` — каждый час на 10-й минуте

### 9.2. Алгоритм `sync_current_snapshot`

Для каждой активной группы:

1. Парсится текущее расписание
2. Берется baseline
3. Вычисляется `change_summary`
4. Сохраняется `current` snapshot
5. Если есть изменения:
   - запись в `change_events`
   - рассылка через `Broadcaster`
   - обновление baseline

### 9.3. `Broadcaster`

- Работает как единая точка отправки уведомлений
- Умеет публиковать уведомления в RabbitMQ
- При включенном RabbitMQ становится producer
- При выключенном RabbitMQ делает прямую отправку в Telegram и VK
- Умеет выборочную рассылку по `schedule_id`
- Для ДЗ использует флаг `homework_notifications_enabled`
- Умеет отдельно уведомлять админов

### 9.4. RabbitMQ в проекте

RabbitMQ используется только для исходящих уведомлений.

Схема такая:

1. scheduler или админка формируют уведомление
2. `Broadcaster` получает текст уведомления
3. `Broadcaster` публикует задачу в очередь `misis_notifications`
4. consumer из `src/message_broker.py` читает сообщения из очереди
5. consumer вызывает отправку в нужную платформу:
   - Telegram
   - VK

Что хранится в очереди:

- `platform`
- `user_id`
- `text`

Пример payload:

```json
{
  "platform": "telegram",
  "user_id": 123456789,
  "text": "Обнаружены изменения в расписании!"
}
```

Преимущества такой схемы:

- уведомления не улетают всем сразу одним пакетом
- проще переживаются всплески нагрузки
- producer и delivery-логика разделены
- бот меньше зависит от мгновенной доступности API Telegram/VK

---

## 10. База данных (SQLite)

### 10.1. Таблицы

- `users` — пользователи, роли, группа, флаги уведомлений
- `schedule_snapshots` — снимки расписания (`current`, `daily_baseline`)
- `change_events` — события изменения расписания
- `homework_entries` — записи ДЗ
- `homework_attachments` — вложения ДЗ
- `linked_accounts` — связь Telegram<->VK аккаунтов
- `link_tokens` — временные коды привязки

### 10.2. Что хранится в слепке

`content_json` содержит:

- `group_name`
- `fetched_at`
- список дней, где для каждого дня — список пар

### 10.3. Слой `Database`

`Database` инкапсулирует весь SQL: инициализацию схемы, CRUD пользователей, snapshots, change events, счетчики пар, привязку аккаунтов.

---

## 11. Счетчики пар

- Список групп и дисциплин хранится в JSON из `LESSON_COUNTERS_PATH`.
- В 23:00 и 23:40 бот создает задачи подсчета по всем группам из JSON, даже если на группу никто не подписан.
- При включенном RabbitMQ задачи идут в durable-очередь `LESSON_COUNTERS_QUEUE`.
- Одна и та же пара за одну дату не засчитывается повторно.

### Формат JSON

См. пример в `storage/lesson_counters.json`.

---

## 12. Telegram-бот: логика и сценарии

Точка сборки: `build_dispatcher(...)`.

Основные сценарии:

1. Регистрация/обновление пользователя при каждом входящем событии
2. Проверка выбранной группы (иначе принудительный выбор)
3. Меню расписания, поиск расписания
4. Просмотр ДЗ
5. Создание ДЗ (для редактора):
   - выбор предмета
   - ввод текста
   - добавление вложений
   - предпросмотр
   - публикация
6. Настройки:
   - toggle уведомлений ДЗ
   - отписка от группы
7. Админка:
   - статус
   - перепарс
   - сохранение baseline
   - пользователи
   - редакторы
   - удаление ДЗ
   - тестовая рассылка

Особенность: используется `context_messages` для «чистого» UX (редактирование/удаление устаревших сообщений вместо спама).

---

## 13. VK-бот: логика и сценарии

Точка сборки: `build_vk_bot(...)`.

Основные элементы:

- `peer_modes` — текущий режим диалога (FSM-подобное состояние)
- `peer_pages` — пагинация списков
- `editor_option_map`, `delete_option_map` — сопоставление кнопок и действий

Сценарии аналогичны Telegram:

- группа/расписание/поиск
- ДЗ просмотр и публикация
- настройки
- админка

Сообщения обрабатываются единым `all_messages_handler`, логика ветвится по состоянию `mode` и тексту кнопок.

---

## 14. Полный справочник функций и методов

Ниже перечислены **все** функции/методы проекта.

### 14.1 `src/main.py`

- `run_telegram_polling(...)` — запуск polling Telegram и привязка Bot к Broadcaster.
- `run_vk_polling(vk_bot)` — запуск polling VK внутри текущего event loop.
- `main()` — полная инициализация приложения.
- `_run_vk()` (внутри `run_vk_polling`) — обертка запуска VK.

### 14.2 `src/config.py`

- `Settings.from_env()` — загрузка и преобразование переменных окружения в dataclass.

### 14.3 `src/models.py`

- `HomeworkDraft.__post_init__()` — гарантирует список `attachments`.

### 14.4 `src/parser.py` (`ScheduleParser`)

- `__init__` — конфиг парсера и URL.
- `build_schedule_url` — сборка URL группы по `schedule_id`.
- `fetch_html` — загрузка HTML страницы группы.
- `fetch_html_from_url` — загрузка HTML произвольной страницы расписания.
- `_get_with_retry` — HTTP GET с ретраями.
- `parse` — парсинг по `schedule_id` (+ hash).
- `parse_from_url` — парсинг по URL (+ hash).
- `parse_html` — извлечение дат и пар из HTML.
- `compute_hash` — SHA256 нормализованного слепка.
- `_date_label_to_iso` — перевод русской даты в ISO.

### 14.5 `src/group_catalog.py` (`GroupCatalog`)

- `__init__` — конфиг каталога групп.
- `ensure_loaded` — ленивое обеспечение загрузки.
- `refresh` — реальная загрузка отделений/групп с сайта.
- `_get_with_retry` — HTTP GET с ретраями.
- `list_groups` — список всех групп.
- `find_group` — поиск группы по имени.
- `get_by_schedule_id` — поиск группы по `schedule_id`.
- `normalize` — нормализация поисковой строки.

### 14.6 `src/schedule_search.py` (`ScheduleSearchCatalog`)

- `__init__` — конфиг поиска.
- `find` — универсальный поиск группы/преподавателя/аудитории.
- `_ensure_preps_loaded` — загрузка справочника преподавателей.
- `_ensure_auds_loaded` — загрузка справочника аудиторий.
- `_get_with_retry` — HTTP GET с ретраями.
- `_find_partial` — частичный матч строки.
- `normalize` — нормализация строки запроса.

### 14.7 `src/schedule_service.py`

`ScheduleFormatter`:

- `format_day` — человекочитаемый день расписания.
- `format_day_card` — HTML-формат дня для Telegram.
- `format_day_plain` — plain-формат дня.
- `format_range` — формат нескольких дней.
- `format_search_snapshot` — формат результата поиска.

`ScheduleComparator`:

- `compare` — сравнение baseline/current и сбор `ChangeSummary`.
- `_day_changed` — сравнение одного дня.

Функции модуля:

- `filter_days` — ближайшие дни из snapshot.
- `get_day_by_offset` — день по смещению из snapshot.
- `get_day_by_offset_from_content` — день по смещению из JSON-контента snapshot.

### 14.8 `src/notifier.py` (`Broadcaster`)

- `__init__` — связывает DB и ботов.
- `broadcast` — универсальная рассылка в TG/VK.
- `broadcast_test_message` — тестовая рассылка.
- `broadcast_homework_update` — рассылка только подписчикам ДЗ.
- `notify_admins` — отправка сообщений администраторам.
- `_broadcast_telegram` — рассылка в Telegram.
- `_broadcast_vk` — рассылка в VK.

### 14.9 `src/scheduler.py` (`ScheduleJobs`)

- `__init__` — создание AsyncIOScheduler.
- `configure` — регистрация Cron jobs.
- `start` — запуск scheduler.
- `save_daily_baseline` — сохранение дневного baseline.
- `save_daily_baseline_fallback` — fallback baseline.
- `sync_current_snapshot` — синхронизация current, сравнение, запись и рассылка.

### 14.10 `src/lesson_counters.py`

- `load_config_file` — чтение JSON счетчиков.
- `sync_config` — синхронизация JSON в БД.
- `configured_schedule_ids` — список групп, по которым надо считать пары.
- `count_today_for_snapshot` — учет сегодняшних пар без дублей.
- `save_vk_url` — скачивание файла из URL (VK).
- `save_vk_message_attachments` — извлечение/сохранение вложений из VK сообщения.
- `_build_relative_path` — формирование безопасного относительного пути.
- `_detect_extension` — определение расширения файла.
- `_sanitize_stem` — очистка имени файла.
- `_build_vk_attachment_string` — сериализация VK attachment-id.
- `_pick_vk_photo_url` — выбор лучшего URL фото.
- `_pick_vk_video_url` — выбор доступного mp4 URL видео.
- `_vk_doc_type` — типизирование VK doc по расширению.
- `_guess_mime_type` — попытка определения MIME.

### 14.12 `src/db.py` (`Database`)

- `__init__` — путь к sqlite.
- `initialize` — создание/миграция схемы.
- `_ensure_column` — безопасное добавление недостающего столбца.
- `upsert_user` — создать/обновить пользователя.
- `list_users` — список пользователей c фильтрами.
- `get_users_for_platform` — пользователи платформы.
- `get_user` — получить одного пользователя.
- `set_user_group` — назначить группу пользователю.
- `clear_user_group` — снять группу.
- `set_editor` — включить/выключить редактора.
- `set_homework_notifications` — переключить подписку на ДЗ.
- `get_users_for_homework_notifications` — выборка подписчиков ДЗ.
- `get_active_groups` — активные группы по пользователям.
- `save_snapshot` — сохранение snapshot.
- `get_latest_snapshot` — последний snapshot по типу/группе.
- `record_change` — запись события изменения.
- `get_last_change` — последнее изменение.
- `count_homework_entries` — количество записей ДЗ.
- `create_homework` — создание ДЗ + вложений.
- `get_homework_for_subject` — последние ДЗ по предмету.
- `delete_homework` — удаление ДЗ.
- `create_link_token` — генерация одноразового кода привязки.
- `get_linked_account` — получить связанный аккаунт.
- `unlink_account` — отвязать аккаунт.
- `consume_link_token` — применить код привязки и создать связь TG/VK.
- `get_homework_attachments` — вложения записи ДЗ.
- `has_baseline_for_date` — проверка baseline на дату.
- `_snapshot_to_dict` — сериализация dataclass snapshot в dict.

### 14.13 `src/telegram_bot.py`

Внешняя функция:

- `build_dispatcher(...)` — собирает Dispatcher, клавиатуры, хелперы и все обработчики.

Внутренние функции-хелперы и обработчики (все):

- `build_homework_subjects_keyboard`
- `build_editors_keyboard`
- `build_homework_preview_keyboard`
- `build_homework_attachment_keyboard`
- `build_admin_homework_subjects_keyboard`
- `build_admin_homework_entries_keyboard`
- `register_message_user`
- `register_callback_user`
- `user_is_admin`
- `user_is_editor`
- `get_user_record`
- `user_has_homework_access`
- `get_saved_snapshot`
- `format_group_prompt`
- `format_search_prompt`
- `build_search_result_keyboard`
- `format_welcome`
- `format_settings_text`
- `build_settings_keyboard`
- `format_admin_panel`
- `build_admin_users_keyboard`
- `empty_day_text`
- `format_snapshot_info`
- `format_admin_status`
- `replace_context_message`
- `send_new_context_message`
- `clear_context_messages`
- `try_delete_message`
- `clear_context_messages_except`
- `try_edit_source_message`
- `safe_callback_answer`
- `safe_edit_message_text`
- `short_error_text`
- `notify_user_about_error`
- `notify_admin_about_error`
- `extract_error_context`
- `prompt_group_selection`
- `ensure_group_selected`
- `prompt_schedule_search`
- `perform_schedule_search`
- `handle_group_input`
- `send_schedule_menu`
- `send_homework_subject_picker`
- `send_homework_entries`
- `send_homework_entry_with_attachments`
- `send_attachment`
- `send_draft_preview_message`
- `send_draft_preview`
- `handle_start`
- `handle_settings_command`
- `handle_rasp_command`
- `handle_homework_command`
- `handle_dz_command`
- `handle_cancel_command`
- `handle_admin_command`
- `handle_menu_start`
- `handle_menu_settings`
- `handle_menu_homework`
- `handle_start_rasp`
- `handle_start_homework`
- `handle_schedule_callback`
- `handle_homework_subject`
- `handle_homework_subject_for_create`
- `handle_add_attachments`
- `handle_save_homework`
- `handle_cancel_homework`
- `handle_settings_callback`
- `handle_admin_callback`
- `handle_editor_toggle`
- `handle_homework_attachment_message`
- `handle_homework_text_shortcut`
- `handle_text_message`
- `handle_telegram_errors`

### 14.14 `src/vk_bot.py`

Внешняя функция:

- `build_vk_bot(...)` — собирает VK Bot, клавиатуры, состояние диалогов и обработчики.

Внутренние функции-хелперы и обработчики (все):

- `make_keyboard`
- `paged_rows`
- `shorten_button_label`
- `short_error_text`
- `notify_user_about_error`
- `notify_admin_about_error`
- `user_is_admin`
- `user_is_editor`
- `user_has_homework_access`
- `fetch_vk_names`
- `sync_vk_user_names`
- `register_user`
- `show_screen`
- `upload_attachment_for_vk`
- `collect_vk_attachments`
- `menu_keyboard`
- `group_prompt_text`
- `schedule_search_prompt_text`
- `schedule_keyboard`
- `search_result_keyboard`
- `homework_view_keyboard`
- `draft_preview_keyboard`
- `draft_attachment_keyboard`
- `settings_keyboard`
- `admin_keyboard`
- `welcome_text`
- `settings_text`
- `schedule_text`
- `homework_text`
- `preview_text`
- `snapshot_line`
- `admin_status_text`
- `show_main_menu`
- `prompt_group_selection`
- `ensure_group_selected`
- `handle_group_input`
- `get_or_fetch_snapshot`
- `perform_schedule_search`
- `handle_vk_errors`
- `show_settings`
- `show_homework_subjects`
- `show_dz_subjects`
- `show_admin_delete_subjects`
- `show_latest_homework`
- `show_draft_preview`
- `publish_homework`
- `subject_by_title`
- `build_editor_keyboard`
- `build_delete_keyboard`
- `all_messages_handler`

---

## 15. Поток данных (сквозной сценарий)

1. Пользователь выбирает группу
2. `users` обновляется в БД
3. По запросу расписания:
   - берется `current` snapshot из БД
   - если нет — парсится сайт и сохраняется snapshot
4. Планировщик регулярно синхронизирует расписание
5. При изменениях:
   - запись в `change_events`
   - `Broadcaster` создает задачи на рассылку
   - задачи уходят в RabbitMQ
   - consumer читает очередь и доставляет сообщения пользователям выбранной группы
---

## 16. Ограничения и важные детали

- В Telegram и VK используются отдельные состояния диалога.
- В Telegram применяются HTML-сообщения (`ParseMode.HTML`) — строки экранируются через `html.escape`.
- Для стабильной работы на VPS обязательно корректно выставлять `APP_TIMEZONE`.

---

## 17. Минимальная проверка после изменений

Проверка синтаксиса:

```bash
python -m compileall src
```
