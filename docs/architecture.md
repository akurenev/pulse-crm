# Архитектура Pulse CRM MVP

## Границы системы

Pulse CRM обслуживает один workspace в интерфейсе, но каждая бизнес-сущность
содержит `workspace_id`. API никогда не принимает workspace из пользовательского
payload: он извлекается из сессии и включается во все запросы. Это сохраняет
возможность безопасно расширить модель позже и предотвращает горизонтальный
доступ к чужим данным уже в MVP.

Production состоит из трёх ресурсов:

1. Timeweb App Platform webapp: FastAPI, статический React build и supervisor.
2. PostgreSQL 17 или 18 DBaaS: доменные данные, сессии, job queue, outbox и
   realtime.
3. Приватный S3: `attachments/`, `imports/`, `exports/`, `backups/`.

## Модули

| Модуль | Ответственность |
|---|---|
| `app.api.auth` | bootstrap, email/password, сессии, CSRF, приглашения и роли |
| `app.api.crm` | компании, контакты, воронки, сделки, поля, задачи и activity |
| `app.integrations` | сообщения, webhook/form, каналы, S3, уведомления и импорт |
| `app.services.events` | атомарные activity/outbox/realtime события |
| `app.services.jobs` | очередь, leases, retry/backoff и supervisor |
| `app.api.events` | replayable SSE по монотонному `event_id` |
| `frontend` | responsive SPA и optimistic UI |

## Воронки и внешние идентификаторы

Администратор может создавать и переименовывать воронки, добавлять открытые
этапы, менять название и цвет этапа и удалять только неиспользуемые объекты.
Удаление блокируется, если на этап или воронку ссылаются сделки, история
переходов, каналы, формы, webhook-и или правила оповещений. Финальные этапы
`won`/`lost` и последний открытый этап защищены.

ID этапа amoCRM уникален только внутри своей воронки: системные status ID могут
повторяться. Поэтому `ExternalEntityMap` использует составной внешний ключ
`pipeline_id:status_id`, а импорт сделки разрешает этап по той же паре. Для
ранее созданных записей поддерживается чтение legacy-сопоставления по одному
status ID; новое сопоставление всегда записывается в составном формате.

## Транзакционный путь изменения

```text
HTTP / inbound event
        │
        ▼
validate identity, role, workspace, version
        │
        ▼
domain row + ActivityEvent + OutboxEvent + RealtimeEvent
        │                 (один COMMIT)
        ▼
HTTP response          supervisor claims outbox/job
                              │
                              ▼
                      external channel / notification
```

Внешний HTTP/SMTP/S3 вызов не выполняется под блокировкой доменной строки.
Результат доставки фиксируется отдельной короткой транзакцией. Повторная
доставка проверяет состояние сообщения или уведомления и безопасно завершается,
если работа уже выполнена.

## Очередь и расписание

`background_jobs` содержит тип, JSON payload, `run_at`, статус, попытки,
`dedupe_key`, owner/until lease и последнюю ошибку. Для claim используется
частичный составной индекс только по queued-заданиям. Supervisor:

- возвращает просроченные leases;
- захватывает небольшую пачку `SKIP LOCKED`;
- commit'ит lease до выполнения обработчика;
- ограничивает время одного запуска и применяет exponential backoff;
- публикует heartbeat для readiness;
- scheduler под advisory lock создаёт идемпотентные задания напоминаний,
  контроля активности, polling и очистки.

Импорт amoCRM хранит cursor и счётчики в `ImportJob`, а каждую страницу создаёт
как отдельное задание. Пауза не отменяет уже выполняющееся короткое задание, но
не позволяет поставить следующую страницу. После успешного завершения отдельное
идемпотентное задание формирует канонический JSON-отчёт в приватном S3; UI
получает его только по короткоживущей подписанной ссылке.

## Поиск и пагинация

Списки сортируются стабильной парой `(created_at, id)` и используют cursor,
а не `OFFSET`. Телефоны и email нормализуются до записи. Для крупной production
БД предусмотрены GIN `tsvector` индексы по именам/названиям и точечные индексы
по нормализованным контактам. JSONB custom fields индексируются только для
реально используемых фильтров: универсальный GIN на каждую JSONB-колонку в MVP
не создаётся.

Индексы `ix_contacts_fulltext` и `ix_deals_fulltext` создаются явным
PostgreSQL DDL в Alembic-миграции. Они перечислены как управляемые миграцией в
`backend/alembic/env.py`, поэтому autogenerate/check не предлагает удалить их
как отсутствующие в ORM metadata. Обычные metadata-индексы продолжают
сравниваться без исключений.

## Защита

- Argon2id, HttpOnly/Secure/SameSite cookies, CSRF и отзыв сессии;
- `owner/admin/manager` с проверкой роли в endpoint;
- optimistic locking для изменяемых записей, HTTP 409 при stale version;
- HMAC-SHA256, replay window и `Idempotency-Key` для generic webhook;
- provider secret для Telegram/MAX webhook;
- AES-GCM для токенов интеграций, master key только в environment;
- MIME/расширение/размер до 20 МБ, запрет архивов и executable;
- authenticated upload в приватный S3 и signed GET с коротким TTL;
- request ID и структурированные логи без тел сообщений и секретов.

## Пределы MVP

Один процесс подходит для заявленной нагрузки при коротких заданиях и
правильных индексах. Сигналом к отдельному worker служат устойчивые очереди
старше пяти минут, saturation CPU/RAM или необходимость нескольких реплик API.
Выделение worker, Redis или Kafka не выполняется без измерений и отдельного ADR.
