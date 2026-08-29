# Runbook: запуск Pulse CRM в Timeweb

Документ предназначен для публичного репозитория и содержит только переносимые
настройки. Фактические домены, IP, имена сетей и бакетов, размеры production-
ресурсов и значения секретов должны храниться вне Git.

## Перед первым deploy

- Webapp, PostgreSQL и приватная BGP-сеть находятся в одном выбранном регионе.
- Используется PostgreSQL 17 или 18; публичный IP БД выключен.
- S3 bucket приватный; созданы префиксы `attachments/`, `imports/`, `exports/`,
  `backups/`; для пилота допустим минимальный стандартный тариф с
  автомасштабированием. Versioning/retention включаются по принятой политике.
- App Platform настроен на окружение Dockerfile, порт 8000 и одну реплику.
  Когда `Dockerfile` находится в корне, поля команды сборки, команды запуска и
  пути к директории проекта в панели оставляются пустыми.
- Production autodeploy выключен; deploy указывает проверенный commit SHA.
- Секреты заданы в variables App Platform, а не в Git или Docker build args.

Минимальная конфигурация webapp и DBaaS подходит для установки, smoke-тестов и
малой пилотной группы. До большого импорта и подключения всей команды проверьте
RAM/CPU, p95 запросов, соединения БД и возраст очереди. Масштабируйте ресурс при
устойчивом дефиците памяти/CPU или очереди старше пяти минут.

Обязательные переменные приведены в `.env.example` и README. В production
нужны отдельные случайные значения `PULSE_SECRET_KEY`, ключа шифрования
интеграций и одноразового bootstrap token.

Используйте только значения из панели конкретного контура. Не переносите в
issues, документацию или скриншоты `PULSE_DATABASE_URL`, S3 keys, OAuth secrets,
bot tokens и значения encryption/bootstrap keys.

## Deploy

1. Убедиться, что CI зелёный для выбранного SHA.
2. Создать ручной backup PostgreSQL перед рискованным релизом.
3. Развернуть SHA. Entrypoint получает advisory lock и выполняет Alembic.
4. Проверить `/health/live`, затем `/health/ready` не менее трёх раз.
   Dockerfile уже содержит внутренний healthcheck `/health/live`; внешний
   мониторинг использует `/health/ready`, потому что он также проверяет БД и
   heartbeat supervisor.
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

## Справка Timeweb

- [Dockerfile и healthcheck App Platform](https://timeweb.cloud/docs/apps/healthcheck-path)
- [Создание приватной BGP-сети](https://timeweb.cloud/docs/vpc/managing-bgp-networks/creating-bgp-networks)
- [Создание PostgreSQL DBaaS](https://timeweb.cloud/docs/dbaas/dbaas-create)
- [Создание приватного S3-бакета](https://timeweb.cloud/docs/s3-storage/manage-storage/create-bucket)
