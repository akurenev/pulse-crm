# Руководство администратора Pulse CRM

Это руководство относится к deploy-ready MVP `v0.1.0`: код приложения,
миграции, фоновые задания и production-контейнер реализованы, но сам репозиторий
не означает, что конкретный production-контур уже создан. Развёртывание в
Timeweb описано в [production runbook](runbooks/timeweb-production.md).

> [!IMPORTANT]
> Репозиторий публичный. Все домены, UUID и реквизиты ниже — плейсхолдеры.
> Фактические connection strings, IP, имена бакетов/сетей, OAuth secrets и
> token должны храниться только в закрытых variables соответствующего контура.

## 1. Первый запуск и bootstrap

До первого старта задайте production-переменные из `.env.example`. Особенно
важны отдельные случайные значения:

- `PULSE_SECRET_KEY` — секрет приложения не короче 32 байт;
- `PULSE_BOOTSTRAP_TOKEN` — одноразовый token не короче 24 символов;
- `PULSE_INTEGRATION_ENCRYPTION_KEY` — Base64 от отдельного 32-байтного ключа;
- `PULSE_INTEGRATION_ENCRYPTION_KEY_ID` — имя активного ключа, например
  `primary`;
- `PULSE_DATABASE_URL` и параметры приватного S3;
- `PULSE_COOKIE_SECURE=true`, `PULSE_JOB_RUNNER_ENABLED=true` и
  `PULSE_ALLOWED_HOSTS='["crm.example.com"]'`.

После deploy сначала проверьте:

```bash
curl -fsS https://crm.example.com/health/live
curl -fsS https://crm.example.com/health/ready
```

Создайте единственный workspace и владельца. Пароль должен содержать не менее
12 символов, а `workspace_slug` — только строчные латинские буквы, цифры и
дефисы.

```bash
curl -i -c pulse.cookies \
  -H 'Content-Type: application/json' \
  -H 'X-Bootstrap-Token: ONE_TIME_BOOTSTRAP_TOKEN' \
  -d '{
    "workspace_name":"Моя компания",
    "workspace_slug":"my-company",
    "email":"owner@example.com",
    "full_name":"Владелец CRM",
    "password":"CHANGE-THIS-LONG-PASSWORD"
  }' \
  https://crm.example.com/api/v1/auth/bootstrap
```

Ответ содержит `csrf_token`, а cookie `pulse_session` устанавливается как
HttpOnly. Для изменяющих API-запросов с cookie передавайте этот token в
`X-CSRF-Token`. После bootstrap удалите `PULSE_BOOTSTRAP_TOKEN` из variables и
перезапустите webapp. Повторный bootstrap возвращает `409`.

Обычный вход выполняется через интерфейс или `POST /api/v1/auth/login`. После
перезагрузки SPA получает свежий CSRF-token через `GET /api/v1/auth/me`.

### PWA-установка

Production-сборка публикует `/manifest.webmanifest`, `/sw.js` и иконки из
`/icons/`. Для установки нужен HTTPS-домен; исключение браузеров для разработки
на `localhost` не следует считать production-конфигурацией. После deploy
проверьте, что reverse proxy не подменяет PWA-файлы HTML-страницей и сохраняет
корректные MIME-типы:

```bash
curl -I https://crm.example.com/manifest.webmanifest
curl -I https://crm.example.com/sw.js
curl -I https://crm.example.com/icons/pulse-crm-192.png
```

Service worker регистрируется только в production-сборке и намеренно не имеет
`fetch`-обработчика или Cache Storage. Он не перехватывает запросы и не хранит
HTML, API-ответы, контакты, сделки или другие клиентские данные; установленной
CRM по-прежнему требуется сеть. Это дополняет серверный `Cache-Control:
no-store` для `/api/*`.

Авторизованному пользователю Chromium показывает кнопку, запускающую системную
установку, а iOS — инструкцию **Поделиться → На экран «Домой»**. Предложение не
показывается в standalone-режиме; отказ сохраняет только время закрытия в
`localStorage` и повторно скрывает подсказку на 30 дней.

## 2. Роли и приглашения

В MVP все сотрудники видят CRM-данные одного workspace:

| Роль | Права |
|---|---|
| `owner` | CRM-данные, все настройки, приглашение `admin`, управление workspace |
| `admin` | CRM-данные, воронки, поля, каналы, уведомления, импорт, приглашение `manager` |
| `manager` | Контакты, компании, сделки, задачи, сообщения и активность; без административных настроек |

Штатный массовый экспорт CRM не входит в MVP. Даже при включённом в будущем
серверном feature flag право на такой процесс зарезервировано только за
`owner`; роли `admin` и `manager` его не получают.

В разделе **Настройки → Пользователи** укажите email и роль. API-эквивалент —
`POST /api/v1/invitations`; сырой token возвращается только в ответе на создание
и действует 72 часа по умолчанию. Ссылка имеет вид:
`https://crm.example.com/accept-invitation?token=TOKEN`.

Администратор не может приглашать другого администратора — это может сделать
только `owner`. Роль `owner` через приглашение не назначается.

## 3. Воронки, поля и обязательность этапов

Раздел **Настройки → Воронки и поля** показывает все активные воронки. Кнопка
**Новая воронка** создаёт три открытых этапа и системные этапы `won`/`lost`.
Название воронки уникально в пределах workspace; одна воронка содержит не более
50 этапов. Новый открытый этап добавляется перед системными `won`/`lost`.
API-контракт доступен по `POST /api/v1/pipelines`:

```json
{
  "name": "Корпоративные продажи",
  "position": 1,
  "stages": [
    {"name":"Новый лид","color":"#4B96F8","position":0,"stage_type":"open"},
    {"name":"В работе","color":"#6D5DF7","position":1,"stage_type":"open"},
    {"name":"Предложение","color":"#20B878","position":2,"stage_type":"open"},
    {"name":"Успешно","color":"#16A36D","position":3,"stage_type":"won"},
    {"name":"Закрыто","color":"#929AAA","position":4,"stage_type":"lost"}
  ]
}
```

Владелец и администратор могут менять структуру воронок:

- переименовать воронку — `PATCH /api/v1/pipelines/{pipeline_id}`;
- добавить открытый этап — `POST /api/v1/pipelines/{pipeline_id}/stages`;
- изменить название и цвет любого этапа — `PATCH /api/v1/stages/{stage_id}`;
- удалить этап — `DELETE /api/v1/stages/{stage_id}`;
- удалить воронку — `DELETE /api/v1/pipelines/{pipeline_id}`.

В изменяющих запросах передавайте актуальный `expected_version`; для `DELETE`
он передаётся как query-параметр. Несовпадение версии возвращает HTTP `409`,
после чего интерфейс обновляет данные и предлагает повторить действие.

Удаление защищено от потери данных. Можно удалить только неиспользуемый открытый
этап: системные этапы `won`/`lost` защищены, а в воронке должен остаться хотя бы
один этап `open`. Этап считается используемым, если на него ссылаются сделки,
история переходов, каналы, формы, webhook-и или правила оповещений. Воронку можно
удалить, только если она не последняя активная и такие зависимости отсутствуют
у самой воронки и всех её этапов.
При любой такой зависимости API возвращает HTTP `409`, а интерфейс показывает
причину отказа. Сначала перенесите сделки и перенастройте связанные маршруты,
затем повторите удаление.

Пользовательские поля создаются через `POST /api/v1/custom-fields`. Типы:
`text`, `number`, `date`, `boolean`, `select`; области: `deal`, `contact`,
`company`. Для `select` список `options` обязателен.
Административное удаление поля сделки —
`DELETE /api/v1/custom-fields/{field_id}`. Сервер снимает флаг активности
определения и удаляет его из всех требований этапов того же workspace.
Уже записанные в `Deal.custom_fields` JSON-значения не удаляются, но поле
больше не возвращается в списке активных определений. Его технический `key`
остаётся зарезервированным: не создавайте новое поле с тем же ключом. Если поле
ранее пришло из amoCRM, последующие импорты могут обновить его скрытые метаданные
и значения, но не активируют локально удалённое определение снова.

Состав обязательных полей этапа целиком заменяет запрос
`PUT /api/v1/stages/{stage_id}/required-fields`:

```json
{
  "fields": [
    {"built_in_key":"amount"},
    {"built_in_key":"assignee_id"},
    {"field_definition_id":"UUID-ПОЛЯ"}
  ]
}
```

Допустимые встроенные ключи: `title`, `company_id`, `contact_ids`,
`assignee_id`, `amount`, `source_id`, `next_purchase_at`. Входящий лид
сохраняется даже с незаполненными полями, но
`PATCH /api/v1/deals/{deal_id}/stage` блокирует переход и возвращает
`missing_required_fields`. Изменяемые записи используют `expected_version`; при
конфликте клиент получает HTTP `409`.

### Сделки, задачи и следующая покупка

На экране сделок доступны Kanban и список; оба вида открывают одну карточку.
Колонки Kanban выстроены в одну строку без переноса и прокручиваются по
горизонтали на телефоне, планшете и десктопе. На десктопе между этапами
работает drag-and-drop; на телефоне этап меняется select-полем карточки.
Табличный вид на телефоне перестраивается в компактные карточки.
Вкладки карточки содержат детали, задачи, переписку и историю.
Пользовательские поля сделки можно свернуть; контакт, компания и теги
редактируются кнопкой-карандашом. Круглая кнопка **+**
служит мобильной командой создания сделки.

При первом открытии воронки UI запрашивает только этапы `open`. Для этапов
`won` и `lost` колонка показывает кнопку с текущим названием этапа. Только после
нажатия UI выполняет отдельный `GET /api/v1/deals` с `pipeline_id`, `stage_id` и
`limit=100`; при переключении воронки явный выбор финальных этапов сбрасывается.
Переключение использует уже загруженные воронки, источники и пользователей, а
связанные задачи обновляются отдельно и не задерживают показ сделок.

Поле **Следующая покупка** принимает дату в карточке сделки и сохраняется через
`PATCH /api/v1/deals/{deal_id}` с `next_purchase_at` и `expected_version`.
Backend создаёт durable `PurchaseSchedule`; scheduler формирует одну задачу и
одно событие напоминания для конкретной сделки и даты. Изменение даты отменяет
предыдущий активный schedule, удаление даты отменяет его, а новую сделку
автоматически не создаёт.

Обычные задачи создаются в разделе **Задачи** или через `POST /api/v1/tasks` и
содержат тип, срок, напоминание, исполнителя и необязательную связь со сделкой,
контактом или компанией. Завершение выполняется `PATCH /api/v1/tasks/{id}` с
текущим `expected_version`. На телефоне статус задач выбирается через нативный
select, а новая задача создаётся круглой кнопкой **+**.

`GET /api/v1/tasks` по умолчанию возвращает только `open` задачи по
25 записей и `next_cursor`. Кнопка **Показать закрытые** добавляет
`include_completed=true`: в этот режим входят статусы `completed` и `cancelled`,
а **Скрыть закрытые** снова запрашивает только открытые. Параметр `scope`
принимает `all`, `today`, `overdue` и `upcoming`; дата для этих фильтров
вычисляется в часовом поясе workspace до cursor-pagination. Явный `status`
имеет приоритет над режимом `include_completed`. Параметр `search` фильтрует
название, описание и имя ответственного до cursor-pagination. UI хранит историю
cursor-ов для кнопок **Назад** и **Далее**; смена фильтра, поискового запроса,
режима закрытых или создание задачи сбрасывает её на первую страницу.

Списки контактов и компаний в UI запрашивают по 25 записей и используют
`next_cursor` соответствующих `GET /api/v1/contacts` и
`GET /api/v1/companies`. UI выполняет запрос только для активной вкладки,
неактивную загружает после переключения, а при ошибке даёт повторить тот же
запрос. Истории переходов хранятся отдельно для двух списков; поиск и создание
записи возвращают соответствующий
список на первую страницу.

Модель и read API контактов пока не хранят `assignee_id`. Поэтому UI показывает
**Не назначен** и не присваивает контакту синтетического ответственного.
API по-прежнему принимает `limit` до 100, а продолжения учитываются политикой
cursor budget из раздела 10.

Заметка в карточке создаётся через `POST /api/v1/deals/{id}/notes`,
`POST /api/v1/contacts/{id}/notes` или `POST /api/v1/companies/{id}/notes`.
Она попадает в неизменяемую ленту вместе с outbox- и realtime-событием в одной
транзакции. История покупок контакта — выигранные связанные сделки — доступна
с cursor pagination через `GET /api/v1/contacts/{id}/purchases`.

## 4. Приватный S3 и вложения

Бакет должен быть приватным. Webapp получает доступ только через
`PULSE_S3_ENDPOINT_URL`, `PULSE_S3_REGION`, `PULSE_S3_BUCKET`,
`PULSE_S3_ACCESS_KEY_ID` и `PULSE_S3_SECRET_ACCESS_KEY`. Публичный read ACL не
нужен. Объекты вложений записываются под
`attachments/{workspace_id}/YYYY/MM/{uuid}/{filename}`.

Поддерживаются изображения, PDF, TXT/CSV и распространённые офисные форматы
DOC(X), XLS(X), PPT(X), ODT/ODS/ODP, RTF до 20 МБ. Архивы и исполняемые файлы
отклоняются. Расширение, MIME type и сигнатура PDF/современных офисных файлов
проверяются вместе.

- `POST /api/v1/messages/{message_id}/attachments` — multipart upload с полем
  `file`;
- `POST /api/v1/deals/{deal_id}/messages/with-attachment` — атомарное создание
  исходящего сообщения, multipart-поля `body` и `file`;
- `GET /api/v1/attachments/{attachment_id}` — метаданные;
- `GET /api/v1/attachments/{attachment_id}/download` — подписанная ссылка на 5
  минут.

В production включите versioning/retention бакета. Не делайте proxy-бакет
публичным: авторизация и проверка `workspace_id` выполняются до выдачи URL.

## 5. Email, Telegram и MAX

Канал создаётся в **Настройки → Каналы** или через
`POST /api/v1/admin/integrations/channels`. Для каждого подключения обязательны
`default_pipeline_id` и `default_stage_id`; можно задать
`default_assignee_id`. `credentials` шифруются AES-GCM и никогда не возвращаются
read API.

### Email

Пример `credentials` общего корпоративного ящика:

```json
{
  "smtp": {
    "host": "smtp.example.com",
    "port": 587,
    "security": "starttls",
    "username": "sales@example.com",
    "password": "SMTP_PASSWORD",
    "from_address": "sales@example.com",
    "subject": "Pulse CRM"
  },
  "imap": {
    "host": "imap.example.com",
    "port": 993,
    "security": "ssl",
    "username": "sales@example.com",
    "password": "IMAP_PASSWORD",
    "mailbox": "INBOX"
  }
}
```

`security` принимает `ssl`, `starttls` или `plain`; `plain` используйте только
в доверенном локальном контуре. IMAP polling ведёт cursor по UID/UIDVALIDITY,
поэтому повторный опрос не создаёт дубликаты. Для email фиксируются только
достоверные статусы `sent`/`failed`.

### Telegram

```json
{"bot_token":"TELEGRAM_BOT_TOKEN","webhook_secret":"RANDOM_PROVIDER_SECRET"}
```

После создания подключения скопируйте его `id` и зарегистрируйте у Telegram
HTTPS webhook:

```text
https://crm.example.com/hooks/v1/telegram/{connection_id}
```

Передайте то же значение как Telegram `secret_token`. Pulse проверяет входящий
заголовок `X-Telegram-Bot-Api-Secret-Token`.

### MAX

```json
{"access_token":"MAX_ACCESS_TOKEN","webhook_secret":"RANDOM_PROVIDER_SECRET"}
```

Webhook URL:

```text
https://crm.example.com/hooks/v1/max/{connection_id}
```

При создании подписки MAX задайте тот же секрет. Pulse проверяет заголовок
`X-Max-Bot-Api-Secret`. Подключения Telegram и MAX работают только как общие
боты компании.

Сначала создайте канал со статусом `disabled`, проверьте реквизиты и URL, затем
переведите его в `active` через `PATCH .../channels/{id}` с
`expected_version`. При неоднозначном совпадении отправитель не объединяется с
существующим контактом автоматически, а диалог получает состояние проверки.

## 6. Generic webhook

Создайте endpoint через UI или
`POST /api/v1/admin/integrations/webhooks`. Значение `secret` возвращается один
раз; сохраните его в secret-хранилище источника. URL:

```text
POST https://crm.example.com/hooks/v1/generic/{slug}
```

Тело — JSON-объект до 2 МБ с хотя бы одним из ключей `contact`, `deal`,
`message`; дополнительно разрешён `custom_fields`:

```json
{
  "contact": {"name":"Анна", "email":"anna@example.com"},
  "deal": {"title":"Заказ кофе", "amount":45000},
  "message": {"text":"Нужна консультация"},
  "custom_fields": {"utm_source":"site"}
}
```

Обязательные заголовки:

- `X-Pulse-Timestamp` — текущий Unix timestamp в секундах; окно повтора ±5
  минут;
- `Idempotency-Key` — уникальная печатная ASCII-строка длиной 1–255;
- `X-Pulse-Signature` — `sha256=<hex>`.

Подпись — HMAC-SHA256 от точных байтов
`<timestamp>.<raw_request_body>`. Нельзя заново сериализовать JSON после
вычисления подписи. Пример проверки отправки:

```bash
body='{"contact":{"name":"Анна","email":"anna@example.com"},"deal":{"title":"Заказ кофе"}}'
timestamp="$(date +%s)"
signature="$(printf '%s.%s' "$timestamp" "$body" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | awk '{print "sha256=" $2}')"
curl -i \
  -H 'Content-Type: application/json' \
  -H "X-Pulse-Timestamp: $timestamp" \
  -H "X-Pulse-Signature: $signature" \
  -H "Idempotency-Key: test-$(date +%s)" \
  --data-binary "$body" \
  https://crm.example.com/hooks/v1/generic/orders-from-site
```

Успешный приём отвечает `202`. Повтор тех же байтов с тем же ключом снова
отвечает `202` и `duplicate: true`; другой payload с уже использованным ключом
возвращает `409`.

## 7. HTML-формы

Форма создаётся в **Настройки → Каналы → HTML-форма** или через
`POST /api/v1/admin/integrations/forms`. Настройте:

- уникальный `slug`, маршрут в воронку и ответственного;
- `fields_schema` с ключами `key`, `label`, `type`, `required`; типы формы:
  `text`, `email`, `phone`, `textarea`, `number`, `boolean`, `select`;
- точный allowlist `allowed_origins`, например `https://www.example.com`;
- отдельное honeypot-поле и сообщение об успехе.

Готовая страница доступна по `GET /forms/{slug}`. Для встраивания добавьте на
разрешённый сайт:

```html
<script src="https://crm.example.com/forms/request-offer/embed.js" async></script>
```

Submit идёт на `POST /forms/{slug}/submit`. Встроенная разметка добавляет
honeypot и `_idempotency_key`; при собственной форме передавайте заголовок
`Idempotency-Key` или скрытое поле `_idempotency_key`. Сервер проверяет `Origin`,
ограничивает частоту через PostgreSQL и отвечает `202` после надёжной записи.

## 8. Оповещения и согласия

В **Настройки → Оповещения** создаются шаблон и правило. Поддерживаемые события:
`lead.created`, `deal.assigned`, `message.inbound.received`, `task.due_soon`,
`task.overdue`, `deal.inactive`, `purchase.due_soon`,
`deal.stage_changed`. Каналы: `in_app`, `email`, `telegram`, `max`.

Правило хранит фильтры по воронке/этапу/источнику, задержку, статических
получателей и флаг `is_enabled`. Сначала создаётся шаблон через
`/api/v1/admin/integrations/notification-templates`, затем правило через
`/api/v1/admin/integrations/notification-rules`. Для отправки email/Telegram/MAX
в workspace должно быть активное подключение соответствующего типа.

Клиентские правила обязаны иметь `require_client_consent=true`. Доставка
выполняется только при активной записи `ContactChannelConsent` с совпадающими
контактом, каналом, нормализованным адресом и purpose `notifications`; отзыв
согласия блокирует последующие отправки.

В UI выберите получателя **Клиент с согласием**, контакт, канал и адрес, затем
запишите проверяемое основание согласия. Такое правило всегда создаётся
выключенным: администратор включает его отдельно после проверки адреса. API для
контролируемого импорта или отзыва согласий:

- `GET /api/v1/contacts/{contact_id}/consents` — история согласий контакта;
- `POST /api/v1/contacts/{contact_id}/consents` — идемпотентное предоставление
  или повторное предоставление согласия;
- `POST /api/v1/contacts/{contact_id}/consents/{consent_id}/revoke` — отзыв.

Пример предоставления согласия:

```json
{
  "channel": "email",
  "address": "client@example.com",
  "purpose": "notifications",
  "source": "html_form",
  "evidence": {
    "form_submission_id": "request-42",
    "checkbox": true
  }
}
```

Каналы согласия — `email`, `telegram`, `max`. Поля `source` и непустое
JSON-доказательство `evidence` обязательны. Не фиксируйте согласие без
проверяемого основания; повторный grant не создаёт дубликат, а revoke сразу
блокирует последующие клиентские доставки.

Сбойные доставки видны по
`GET /api/v1/admin/notification-deliveries?status=failed`; после исправления
канала их можно повторить через
`POST /api/v1/admin/notification-deliveries/{delivery_id}/retry`.

## 9. OAuth и импорт amoCRM

В amoCRM создайте интеграцию и зарегистрируйте точный Redirect URI:

```text
https://crm.example.com/api/v1/admin/integrations/amocrm/oauth/callback
```

URI должен использовать HTTPS и дословно совпадать с `redirect_uri` в запросе.
Начало подключения —
`POST /api/v1/admin/integrations/amocrm/oauth/start`:

```json
{
  "client_id": "AMO_INTEGRATION_ID",
  "client_secret": "AMO_CLIENT_SECRET",
  "redirect_uri": "https://crm.example.com/api/v1/admin/integrations/amocrm/oauth/callback",
  "allowed_referers": ["example.amocrm.ru"]
}
```

Откройте полученный `authorization_url` в браузере до `expires_at` (state живёт
10 минут). Callback принимает параметры `state`, `code` и `referer`, проверяет
одноразовый state и allowlist аккаунта, затем обменивает code на token. Client
secret, access token и одноразово ротируемый refresh token хранятся только в
зашифрованном виде. Статус доступен по
`GET /api/v1/admin/integrations/amocrm/connection`; отключение —
`POST .../amocrm/disconnect` с `expected_version`.

Для полной репетиции запустите:

Запрос: `POST /api/v1/admin/integrations/imports/start`.

```json
{
  "entity_type": "all",
  "dry_run": true,
  "user_mapping": {"AMO_USER_ID":"PULSE_USER_UUID"}
}
```

`all` последовательно обрабатывает: воронки, этапы, пользователей,
пользовательские поля, компании, контакты, сделки, открытые задачи и обычные
заметки. Для компаний, контактов и сделок сохраняются названия тегов из
`_embedded.tags`; пустые и повторяющиеся значения отбрасываются, порядок
сохраняется, максимум — 100 тегов по 100 символов. Страница содержит не более
250 объектов. API-запрос к amoCRM выполняется вне транзакции, а cursor и counts
сохраняются после каждой страницы.

Серверный поиск компаний, контактов и сделок учитывает сохранённые теги. Для
сделок он также ищет по названию, значениям пользовательских полей и источнику,
поэтому результат не ограничивается уже загруженной страницей Kanban.

Формат и разделение справочников тегов по типам сущностей описаны в
[официальной документации amoCRM](https://www.amocrm.ru/developers/content/crm_platform/tags-api).

Проверьте `counts` и сопоставление пользователей. Затем повторите запрос с
`dry_run: false`. `ExternalEntityMap` обеспечивает повторный запуск без дублей.
Чаты, звонки, файлы и полный журнал изменений не импортируются.

Fingerprint сделок имеет версию схемы. Первый повторный импорт после добавления
поддержки тегов пересматривает уже сопоставленные сделки и заполняет их теги;
следующие неизменившиеся страницы снова пропускаются как `unchanged`. Если набор
тегов в amoCRM изменён или очищен, Pulse заменяет сохранённый набор, а не
объединяет его со старым.

Внешний ID этапа хранится как `pipeline_id:status_id`. Это важно, потому что
одинаковые системные status ID закономерно встречаются в нескольких воронках
amoCRM. При импорте сделки этап ищется по той же паре, а не глобально по одному
status ID. Для старых сопоставлений по одному ID сохранена совместимость;
повторный запуск создаёт составное сопоставление и не дублирует этап.

- список: `GET /api/v1/admin/integrations/imports`;
- пауза: `POST .../imports/{id}/pause` с `{"expected_version":N}`;
- продолжение после `paused` или `failed`: `POST .../imports/{id}/resume` с
  текущим `expected_version`;
- итоговый JSON-отчёт: `GET .../imports/{id}/report` после статуса `succeeded`.

Пауза применяется между страницами; уже подтверждённая страница не откатывается.
После исправления причины failed-запуск следует продолжить, а не создавать
заново: сохранённый cursor и идемпотентные внешние сопоставления защищают от
повторного создания уже обработанных сущностей.
Отчёт записывается в приватный S3 по ключу
`imports/{workspace_id}/{import_id}/report.json`; endpoint возвращает временную
подписанную ссылку на пять минут. В UI она доступна по кнопке **Скачать отчёт**.
Полный production cutover описан в
[runbook amoCRM](runbooks/amocrm-cutover.md).

## 10. Защита данных и политика экспорта

В MVP не реализованы UI, endpoint или background job для массового экспорта
CRM-данных. Дополнительная серверная защита включена по принципу deny by
default:

- `PULSE_CRM_EXPORT_ENABLED=false` — безопасное значение по умолчанию;
- будущий процесс экспорта обязан использовать owner-only dependency и
  дополнительно проверять этот feature flag на сервере;
- только владелец видит эффективное состояние через
  `GET /api/v1/admin/security/export-policy`; одному включению переменной
  недостаточно, чтобы создать выгрузку в текущей версии;
- ответы `/api/*` отправляются с `Cache-Control: no-store`;
- SSE отдаёт только идентификаторы для обновления UI и не повторяет тела
  заметок, сообщений, адреса или доказательства согласий;
- фоновые задания имеют `workspace_id`, а административный список и повтор
  задания, а также само исполнение доменного задания ограничены текущим
  workspace;
- первая страница CRM-списка остаётся доступной для обычного обновления UI, а
  продолжение cursor-pagination учитывается отдельно для пользователя и вида
  списка; по умолчанию разрешено 20 продолжений за 15 минут, затем API отвечает
  HTTP 429 с `Retry-After` и один раз журналирует превышение;
- выдача временной ссылки на вложение или итоговый отчёт импорта записывается
  в журнал активности без URL, имени бакета и секретов.

Временные S3-ссылки действуют пять минут, однако уже открытый файл находится за
пределами контроля приложения. Абсолютно запретить копирование данных
авторизованным пользователем невозможно: он может переписать видимую карточку,
автоматизировать разрешённые запросы или сделать снимок экрана. Поэтому эта
политика предотвращает штатный bulk export, уменьшает кэширование и оставляет
аудит чувствительных скачиваний, но не заменяет организационные меры и DLP.

Не включайте `PULSE_CRM_EXPORT_ENABLED` до появления отдельного проверенного
процесса с явным подтверждением владельца, лимитами, аудитом создания и
скачивания, коротким TTL файла и автоматическим удалением объекта.

Лимит последовательного чтения настраивается переменными
`PULSE_CURSOR_PAGE_BUDGET` и `PULSE_CURSOR_PAGE_WINDOW_SECONDS`. Не завышайте их
без анализа журнала и реального размера страниц. Лимит сдерживает быстрый обход,
но не является полной защитой от медленного или ручного копирования.

## 11. Ошибки, health и backup

Администратор видит terminal failed jobs в **Настройки → Импорт amoCRM → Ошибки
фоновых заданий**. API:

- `GET /api/v1/admin/jobs?status=failed`;
- `POST /api/v1/admin/jobs/{job_id}/retry` — переводит failed job обратно в
  `queued`, сбрасывает attempts и lease;
- `GET /api/v1/admin/notification-deliveries?status=failed` — отдельные доставки.

Повторяйте задание только после устранения причины: неверного channel token,
недоступного S3/SMTP, отключённого amoCRM или ошибки данных.

`GET /health/live` проверяет процесс. `GET /health/ready` проверяет PostgreSQL и
heartbeat внутреннего supervisor; при недоступной БД или stale runner отвечает
`503`. Внешний мониторинг должен опрашивать `/health/ready` минимум из двух
регионов. Дополнительно нужны алерты на очередь старше пяти минут, рост failed
jobs/deliveries, 5xx, p95 API и заполнение PostgreSQL.

Политика резервного копирования:

1. Ежедневный физический backup PostgreSQL, хранение минимум 14 копий.
2. Отдельный `pg_dump` перед импортом amoCRM и рискованной миграцией.
3. Versioning/retention приватного S3; объекты `attachments/` нельзя удалять
   раньше бизнес-политики хранения.
4. Ежемесячное восстановление последней БД в отдельный временный PostgreSQL,
   применение миграций и сверка counts.
5. После тестового повторного deploy проверьте чтение старого S3-объекта через
   подписанную ссылку.

Никогда не проверяйте восстановление поверх единственной production-БД. При
отказе supervisor можно временно задать `PULSE_JOB_RUNNER_ENABLED=false` и
перезапустить webapp; это останавливает polling/dispatch/import, но не должно
использоваться как штатный режим.
