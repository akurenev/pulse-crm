# Pulse CRM

Pulse CRM — открытая CRM-система для небольшой команды продаж. Проект развивает
привычную модель работы с несколькими воронками, сделками, клиентами, задачами и
диалогами, но не копирует фирменный стиль или материалы amoCRM.

В репозитории реализован deploy-ready MVP: единый production-образ, миграции,
интеграции, PostgreSQL-backed job runner, тесты и эксплуатационная документация
готовы к развёртыванию. Это не означает, что какой-либо production-контур уже
создан. Целевая конфигурация — до 20 пользователей, 100&nbsp;000 контактов,
50&nbsp;000 сделок и около 1 млн событий и сообщений.

> [!IMPORTANT]
> Репозиторий публичный. Домены, IP-адреса, имена облачных ресурсов, строки
> подключения и значения секретов конкретного production-контура здесь не
> публикуются. Все адреса и реквизиты в документации — безопасные плейсхолдеры.

## Возможности MVP

- контакты, компании и сделки с тегами, заметками и историей покупок;
- несколько воронок, адаптивные Kanban и list views, карточка сделки;
- создание и переименование воронок, добавление и редактирование этапов,
  защищённое удаление неиспользуемых этапов и воронок;
- встроенные и пользовательские поля, обязательность полей на этапе;
- задачи, напоминания, следующая покупка и журнал активности;
- ручные лиды, email, Telegram-бот, MAX-бот, HMAC-webhook и HTML-форма;
- правила уведомлений для команды и клиентов, персональные Web Push для PWA;
- учёт и отзыв подтверждённых согласий клиентов по каждому каналу;
- возобновляемый идемпотентный импорт из amoCRM: воронки и этапы,
  пользователи и поля, компании, контакты, сделки, их теги, открытые задачи и
  заметки;
- штатный массовый экспорт CRM отсутствует в MVP; будущий серверный процесс
  зарезервирован только для владельца и выключен по умолчанию; чувствительные
  API-ответы запрещено кэшировать, а быстрое последовательное чтение списков
  ограничивается и журналируется;
- роли `owner`, `admin` и `manager`;
- интерфейс для mobile, tablet и desktop.

Монетизация, телефония, SaaS-регистрация, личные аккаунты мессенджеров,
двусторонняя синхронизация amoCRM и сложные no-code сценарии не входят в MVP.

## Архитектура

Production намеренно состоит только из трёх ресурсов: одного webapp в Timeweb
App Platform, одной PostgreSQL DBaaS и одного приватного S3-бакета.

```text
Browser / webhooks / bots / email
               │
               ▼
┌──────────────────────────────────────┐
│            Pulse webapp              │
│ FastAPI + React SPA + job supervisor │
└──────────────┬───────────────┬───────┘
               │               │
               ▼               ▼
        PostgreSQL 17/18    Private S3
```

React собирается внутри multi-stage Dockerfile и обслуживается FastAPI с того
же origin. Один процесс Uvicorn обслуживает REST, SSE и входящие webhooks. В
FastAPI lifespan работает supervisor фоновых циклов. Очередь заданий,
расписание, leases, transactional outbox и realtime-курсоры хранятся в
PostgreSQL; Redis, Celery и отдельный worker не требуются. Вложения и отчёты
хранятся в приватном S3.

Основные технологические решения:

- Python 3.13, FastAPI, SQLAlchemy 2, psycopg 3, Alembic;
- React, TypeScript, Vite, TanStack Query и Tailwind CSS;
- PostgreSQL `FOR UPDATE SKIP LOCKED` для заданий и `LISTEN/NOTIFY` для SSE;
- cursor pagination, optimistic locking и обязательная фильтрация по
  `workspace_id`;
- миграции при старте контейнера под PostgreSQL advisory lock;
- один Uvicorn worker и одна реплика webapp в MVP.

## Структура репозитория

```text
backend/                 FastAPI, модели, миграции и backend-тесты
frontend/                React SPA и browser-тесты
scripts/                 контейнерный запуск и безопасные миграции
.github/workflows/       CI
Dockerfile               production-образ всего webapp
docker-compose.yml       локальный PostgreSQL, MinIO, Mailpit и webapp
CHANGELOG.md              журнал заметных изменений
```

## Быстрый локальный запуск

Потребуются Docker Engine с Compose v2 и свободные порты 8000, 5432, 9000,
9001, 1025 и 8025.

```bash
cp .env.example .env
docker compose up --build
```

После старта доступны:

- приложение — <http://localhost:8000>;
- OpenAPI — <http://localhost:8000/docs>;
- Mailpit — <http://localhost:8025>;
- MinIO Console — <http://localhost:9001>.

Первый владелец создаётся запросом `POST /api/v1/auth/bootstrap`: передайте
`PULSE_BOOTSTRAP_TOKEN` в заголовке `X-Bootstrap-Token`, а в JSON — поля
`workspace_name`, `workspace_slug`, `email`, `full_name` и `password`. Точный
контракт и пример доступны в OpenAPI. Endpoint работает только до создания
первого workspace. После bootstrap замените или удалите token и перезапустите
webapp. Значения в `.env.example` предназначены только для локальной разработки.

Остановить окружение:

```bash
docker compose down
```

Удаление локальных volume с базой и объектами выполняется только явно:

```bash
docker compose down --volumes
```

## Запуск backend и frontend без контейнера webapp

Инфраструктуру можно оставить в Docker, а процессы приложения запускать на
хосте:

```bash
cp .env.example .env
docker compose up -d postgres minio minio-init mailpit

python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e "backend[dev]"
set -a
source .env
set +a
alembic -c backend/alembic.ini upgrade head
uvicorn app.main:app --app-dir backend --reload
```

Во втором терминале:

```bash
cd frontend
npm ci
npm run dev
```

Vite dev server использует свой адрес, а production build всегда обслуживается
из `backend/static` единым FastAPI-приложением.

## Проверки

Основные команды совпадают с обязательными GitHub Actions jobs:

```bash
ruff check backend/app backend/tests
mypy backend/app
python -m pytest backend/tests

# Проверка схемы выполняется на отдельной временной PostgreSQL.
export TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE'
PULSE_DATABASE_URL="$TEST_DATABASE_URL" alembic -c backend/alembic.ini upgrade head
PULSE_DATABASE_URL="$TEST_DATABASE_URL" alembic -c backend/alembic.ini check

cd frontend
npm run lint
npm run typecheck
npm run test
npm run build
npm run test:e2e

cd ..
docker build -t pulse-crm:local .
```

GitHub Actions поднимает временные PostgreSQL и S3-compatible MinIO. PostgreSQL
используется для `upgrade head` и `alembic check`, а тесты приложения создают
собственную изолированную тестовую БД. Никакие production-реквизиты CI не нужны.

## Развёртывание в Timeweb

### 1. Создайте инфраструктуру

1. Выберите один доступный регион и разместите в нём webapp, PostgreSQL и
   приватную BGP-сеть.
2. Создайте PostgreSQL 17 или 18 DBaaS без публичного IP.
3. Создайте приватный S3-бакет стандартного класса; для пилота допустим
   минимальный тариф с автомасштабированием.
4. Создайте одно приложение App Platform из этого GitHub-репозитория с
   окружением `Dockerfile`, портом контейнера `8000` и одной репликой.
5. Подключите webapp и PostgreSQL к одной приватной BGP-сети.

Не создавайте Redis, отдельный worker или scheduler: фоновые задачи выполняет
supervisor внутри единственного webapp.

Минимальные тарифы подходят для установки, smoke-тестов и небольшой пилотной
группы, но не являются гарантией целевой нагрузки. Перед большим импортом и
полноценным запуском ориентируйтесь на RAM/CPU, p95 API и возраст очереди;
увеличьте webapp или DBaaS при устойчивом дефиците ресурсов.

### 2. Настройте переменные webapp

Обязательный минимум:

| Переменная | Значение |
|---|---|
| `PULSE_ENVIRONMENT` | `production` |
| `PULSE_DATABASE_URL` | приватный `postgresql+psycopg://…` URL |
| `PULSE_SECRET_KEY` | случайный секрет не короче 32 байт |
| `PULSE_INTEGRATION_ENCRYPTION_KEY` | Base64 от отдельного случайного 32-байтного ключа AES-GCM |
| `PULSE_INTEGRATION_ENCRYPTION_KEY_ID` | идентификатор активного ключа, например `primary` |
| `PULSE_WEB_PUSH_VAPID_PUBLIC_KEY` | опционально: открытый Base64URL-ключ P-256 для Web Push; задаётся вместе с двумя следующими переменными |
| `PULSE_WEB_PUSH_VAPID_PRIVATE_KEY` | опционально: закрытый Base64URL-ключ P-256; хранить только как secret |
| `PULSE_WEB_PUSH_VAPID_SUBJECT` | опционально: контакт оператора VAPID, например `mailto:admin@example.com` |
| `PULSE_BOOTSTRAP_TOKEN` | временный одноразовый token; удалить после создания владельца |
| `PULSE_COOKIE_SECURE` | `true` |
| `PULSE_CRM_EXPORT_ENABLED` | `false`; не включайте до реализации и проверки отдельного owner-only процесса экспорта |
| `PULSE_CURSOR_PAGE_BUDGET` | `20`; число продолжений cursor-pagination на пользователя и тип списка в одном окне |
| `PULSE_CURSOR_PAGE_WINDOW_SECONDS` | `900`; окно лимита последовательного чтения списков |
| `PULSE_ALLOWED_HOSTS` | JSON-массив, например `["crm.example.com"]` |
| `PULSE_JOB_RUNNER_ENABLED` | `true` |
| `PULSE_RUN_MIGRATIONS` | `true` |
| `PULSE_STATIC_DIR` | `/app/backend/static` |
| `PULSE_S3_ENDPOINT_URL` | S3 endpoint Timeweb |
| `PULSE_S3_REGION` | регион бакета |
| `PULSE_S3_BUCKET` | имя приватного бакета |
| `PULSE_S3_ACCESS_KEY_ID` | access key сервисного аккаунта |
| `PULSE_S3_SECRET_ACCESS_KEY` | secret key сервисного аккаунта |

Секреты задаются только через variables/secrets App Platform. Их нельзя
добавлять в репозиторий, Docker image, build arguments, документацию,
скриншоты или логи. Для генерации секретов можно использовать
`openssl rand -hex 32` на доверенной машине.

Web Push остаётся выключенным, пока не заданы все три VAPID-переменные. Одну
стабильную пару для контура создайте на доверенной машине после установки
backend-зависимостей:

```bash
./.venv/bin/python scripts/generate_vapid_keys.py --subject mailto:admin@example.com
```

Если виртуальное окружение уже активировано через `source .venv/bin/activate`,
ту же команду можно запускать с `python`. Системный alias `python` в macOS по
умолчанию может отсутствовать.

Открытый ключ можно передать браузеру, закрытый необходимо сохранить только в
secret variables. Замена пары потребует повторной подписки устройств.

### 3. Выпустите версию

1. Дождитесь успешного CI для конкретного commit в `main`.
2. Отключите production autodeploy и вручную разверните этот commit.
3. Entrypoint выполнит Alembic-миграции под advisory lock, затем запустит один
   Uvicorn worker.
4. Настройте HTTPS-домен. Dockerfile уже содержит внутренний healthcheck
   `/health/live`; внешний мониторинг должен проверять `/health/ready`.
5. Создайте владельца через bootstrap API, затем удалите bootstrap token.
6. Выполните smoke-тест источников и исходящих каналов.

Перед импортом amoCRM или рискованной миграцией сделайте логический backup.
Включите ежедневные backup PostgreSQL с хранением не менее 14 копий,
версионирование/retention S3 и внешний мониторинг `/health/ready`. Откат
приложения должен использовать предыдущий проверенный commit; миграции
разрабатываются по expand/contract и не требуют отката схемы.

## Документация проекта

- руководство пользователя — [docs/user-guide.md](docs/user-guide.md);
- руководство администратора — [docs/admin-guide.md](docs/admin-guide.md);
- архитектура и границы MVP — [docs/architecture.md](docs/architecture.md);
- ADR единого webapp — [docs/adr/0001-single-webapp.md](docs/adr/0001-single-webapp.md);
- production runbook — [docs/runbooks/timeweb-production.md](docs/runbooks/timeweb-production.md);
- перенос из amoCRM — [docs/runbooks/amocrm-cutover.md](docs/runbooks/amocrm-cutover.md);
- журнал изменений — [CHANGELOG.md](CHANGELOG.md);
- дизайн-система — [docs/design/design-system.md](docs/design/design-system.md);
- правила участия — [CONTRIBUTING.md](CONTRIBUTING.md);
- политика безопасности — [SECURITY.md](SECURITY.md);
- лицензия — [Apache License 2.0](LICENSE).

## Лицензия

Pulse CRM распространяется по лицензии Apache License 2.0.
