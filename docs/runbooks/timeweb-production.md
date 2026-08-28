# Runbook: запуск Pulse CRM в Timeweb

## Перед первым deploy

- PostgreSQL 17 и webapp находятся в одной приватной BGP-сети, публичный IP БД
  выключен.
- S3 bucket приватный; созданы префиксы `attachments/`, `imports/`, `exports/`,
  `backups/`, включены versioning/retention.
- App Platform настроен на Dockerfile, порт 8000, одну реплику и не менее
  2 vCPU / 4 ГБ RAM.
- Production autodeploy выключен; deploy указывает проверенный commit SHA.
- Секреты заданы в variables App Platform, а не в Git или Docker build args.

Обязательные переменные приведены в `.env.example` и README. В production
нужны отдельные случайные значения `PULSE_SECRET_KEY`, ключа шифрования
интеграций и одноразового bootstrap token.

## Deploy

1. Убедиться, что CI зелёный для выбранного SHA.
2. Создать ручной backup PostgreSQL перед рискованным релизом.
3. Развернуть SHA. Entrypoint получает advisory lock и выполняет Alembic.
4. Проверить `/health/live`, затем `/health/ready` не менее трёх раз.
5. Проверить login, чтение воронки, создание/перемещение тестовой сделки и SSE.
6. Проверить upload/download тестового вложения и удалить только тестовую запись.
7. Для первого запуска выполнить одноразовый `/api/v1/auth/bootstrap`, затем
   удалить bootstrap token и перезапустить webapp.

## Наблюдение

Алерты обязательны для:

- 5xx или недоступности `/health/ready` из двух регионов;
- stale supervisor heartbeat;
- возраста старейшего queued job более пяти минут;
- роста terminal `failed` jobs/deliveries;
- заполнения диска БД, аномального числа соединений и p95 API;
- ошибок IMAP/provider healthcheck.

## Backup и восстановление

- Физический PostgreSQL backup ежедневно, минимум 14 копий.
- `pg_dump` перед импортом amoCRM и опасными миграциями.
- Ежемесячно восстановить последнюю копию в отдельную тестовую БД, запустить
  миграции и сверить counts/checksums критичных таблиц.
- Для S3 проверить, что объект из старой версии читается через signed URL после
  повторного deploy; жизненный цикл не должен удалять вложения раньше политики.

## Откат

1. Выключить фоновые циклы feature flag, если они усугубляют проблему.
2. Перевести приложение в read-only, если запись небезопасна.
3. Вернуть Telegram/MAX/generic webhook на прежнюю систему.
4. Развернуть предыдущий проверенный SHA. Миграции обязаны быть
   expand/contract-совместимыми и не требуют downgrade.
5. Если повреждены данные, восстановить БД в новый экземпляр, проверить её и
   только затем переключить webapp. Не восстанавливать поверх единственной
   production-копии.

