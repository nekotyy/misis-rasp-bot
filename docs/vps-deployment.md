# Деплой на выделенном VPS

Рабочий каталог на сервере — `/opt/misis-rasp-bot`, ветка production — `main`.
Для GitHub используется отдельный read-only deploy key: приватная часть остаётся
на сервере, публичная добавляется в Deploy keys репозитория. Ключ для входа
администратора по SSH должен быть отдельным.

Docker Engine и Compose устанавливаются из официального APT-репозитория Docker.
Сервис Docker включён при загрузке. На небольшом VPS предусмотрен swap, а размер
логов Docker и journald ограничен. Автоматически устанавливаются обновления
безопасности Debian; обновление приложения выполняется отдельно.

## Сеть и секреты

В серверном `.env` используются:

```env
WEB_BIND_IP=127.0.0.1
RABBITMQ_BIND_IP=127.0.0.1
WEB_COOKIE_SECURE=true
WEB_ENFORCE_SECURITY=1
COMPOSE_PROFILES=
COMPOSE_FILE=docker-compose.yml:/etc/misis-rasp-bot/compose.override.yml
```

Файл `/etc/misis-rasp-bot/compose.override.yml` содержит серверные настройки,
например сохранённый hostname RabbitMQ после переноса. Если дополнительных
настроек нет, его содержимое — `services: {}`. При использовании `COMPOSE_FILE`
файл должен существовать до запуска Compose. Выполняй команды Compose из
`/opt/misis-rasp-bot`.

Доступ к `.env` и приватным ключам — `0600`, к каталогу с резервными архивами —
`0700`. Секреты, архивы и runtime не добавляются в Git. Firewall разрешает вход
по SSH. Порты `8080`, `5672` и `15672` публикуются только на loopback; одного
firewall недостаточно для защиты опубликованных Docker-портов.

Для входа в админку с рабочего компьютера:

```bash
ssh -N -L 127.0.0.1:18080:127.0.0.1:8080 root@<server-ip>
```

Открой `http://127.0.0.1:18080/login`, пока SSH-соединение активно. Существующие
логины и пароли сохраняются. Для localhost приложение поддерживает вход по HTTP
внутри зашифрованного SSH-туннеля без отключения production cookie policy.
Публичный доступ требует отдельной настройки домена, HTTPS и reverse proxy из
основной инструкции.

## Автозапуск и обслуживание

Стек запускается через `/etc/systemd/system/misis-rasp-bot.service`:

```ini
[Unit]
Description=MISIS schedule bot and local admin dashboard
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/misis-rasp-bot
ExecStart=/usr/bin/docker compose up --detach --no-build bot web rabbitmq
ExecStop=/usr/bin/docker compose stop --timeout 60 bot web rabbitmq
TimeoutStartSec=180
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

Образы должны быть собраны или загружены до первого запуска:

```bash
systemctl daemon-reload
systemctl enable --now misis-rasp-bot.service
cd /opt/misis-rasp-bot
docker compose ps
docker compose logs --tail 100 bot web
```

`active (exited)` у oneshot-сервиса — ожидаемое состояние. Работу контейнеров
проверяй через `docker compose ps`; их перезапуск после сбоя обеспечивает
`restart: unless-stopped`. Штатная остановка всего приложения:

```bash
systemctl stop misis-rasp-bot.service
```

Для обновления после необходимых проверок и резервного копирования:

```bash
cd /opt/misis-rasp-bot
git status --short
git pull --ff-only origin main
docker compose config --quiet
docker compose build bot web
docker compose up --detach --no-build bot web rabbitmq
docker compose ps
```

Перед обновлением рабочее дерево должно быть чистым. При ошибке Git или сборки
остановись и устрани её до пересоздания контейнеров. Изменения `.env` применяются
через `docker compose up`, поскольку обычный `docker restart` не обновляет
окружение контейнера. Автоматический запуск сервиса не делает `git pull`.

## Перенос с другого сервера

1. Зафиксируй текущий Git SHA, ID образов, bind mounts и имя Docker volume
   RabbitMQ. Подготовь сервер и те же образы до остановки рабочего бота.
2. Создай согласованную копию SQLite через backup API и проверь `PRAGMA quick_check`.
3. Останови бот и вебку на исходном сервере, затем RabbitMQ. Одновременно два
   экземпляра с теми же токенами запускать нельзя.
4. Перенеси окончательный `.env`, весь `runtime/`, каталог сертификатов и
   остановленный volume RabbitMQ. Включи скрытые файлы, VK cursor, cookies Gemini,
   `outbox_files/`, старые резервные копии и SQLite WAL/SHM, если они присутствуют.
   Не подменяй рабочую базу копией из локального checkout.
5. При переносе всего volume RabbitMQ сохрани hostname исходного контейнера
   в серверном override: каталог Mnesia и имя RabbitMQ-ноды зависят от него.
   Используй тот же образ брокера и сохрани владельцев файлов volume.
6. До старта сравни контрольные суммы всех перенесённых файлов, целостность
   SQLite, число записей и версию миграции. Храни архивы с ограниченным доступом
   вне Git и вне runtime работающего приложения.
7. Запусти сервисы на новом сервере. Проверь RabbitMQ, подключение обоих ботов,
   локальный вход в админку и отсутствие доступа к внутренним портам извне.
   Убедись, что бот на старом сервере остановлен и не запускается после перезагрузки.

Для RabbitMQ override имеет вид:

```yaml
services:
  rabbitmq:
    hostname: <source-rabbitmq-hostname>
```

Сохраняй исходные данные и архивы для восстановления. После начала работы нового
бота старые данные уже могут отставать: возврат прежней SQLite способен повторить
отправленные сообщения. При обратном переносе сначала останови новый экземпляр
и перенеси его актуальные данные, затем запускай старый сервер.
