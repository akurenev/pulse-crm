# Runbook: перенос из amoCRM

## OAuth-подключение

1. В настройках интеграции amoCRM зарегистрировать точный Redirect URI:

   ```text
   https://crm.example.ru/api/v1/admin/integrations/amocrm/oauth/callback
   ```

   URI должен использовать HTTPS и дословно совпадать с URI, который Pulse
   передаст при обмене code.
2. От имени `owner` или `admin` вызвать
   `POST /api/v1/admin/integrations/amocrm/oauth/start` с `client_id`,
   `client_secret`, `redirect_uri` и allowlist аккаунтов
   `allowed_referers`, например `["mycompany.amocrm.ru"]`.
3. Открыть возвращённый `authorization_url` в браузере до `expires_at`. OAuth
   state одноразовый и истекает через 10 минут; URL содержит
   `mode=post_message`.
4. amoCRM вернёт на callback `state`, `code` и `referer`. Pulse проверит state и
   allowlist, обменяет code через `/oauth2/access_token`, зашифрует client
   secret, access token и refresh token. Refresh token ротируется автоматически
   до истечения access token.
5. Проверить `GET /api/v1/admin/integrations/amocrm/connection`: статус должен
   быть `connected`, а `account_domain` — ожидаемым поддоменом amoCRM.

Не передавать OAuth code/token в логи, issue, shell history или документы.
Повторное подключение создаёт новую зашифрованную пару токенов. Для отключения
использовать `POST /api/v1/admin/integrations/amocrm/disconnect` с текущим
`expected_version`.

## Репетиция

1. Создать OAuth-подключение по процедуре выше и проверить статус.
2. Запустить `POST /api/v1/admin/integrations/imports/start` с
   `{"entity_type":"all","dry_run":true,"user_mapping":{...}}`. Режим
   `all` последовательно проходит воронки, этапы, пользователей,
   пользовательские поля, компании, контакты, сделки, открытые задачи и обычные
   заметки.
3. Дождаться `succeeded`, получить `counts` и ошибки сопоставления без создания
   бизнес-сущностей. Статус читается по
   `GET /api/v1/admin/integrations/imports/{id}`.
4. Зафиксировать ручное `user_mapping` из внешних amoCRM user ID в Pulse user
   UUID; не назначенные записи
   направить владельцу workspace.
5. Выполнить полный импорт с `dry_run:false` в отдельную тестовую PostgreSQL.
6. Сверить количества по типам, воронкам и этапам, суммы сделок и открытые
   задачи; вручную проверить 100 случайных записей.
7. Повторить импорт и убедиться, что `ExternalEntityMap` не допускает дублей.

Импорт сохраняет cursor после каждой страницы (до 250 записей). Пауза —
`POST .../imports/{id}/pause`, продолжение —
`POST .../imports/{id}/resume`; оба запроса принимают текущий
`expected_version`. Пауза действует между страницами и не откатывает уже
подтверждённые данные.

Не переносятся чаты, звонки, файлы и полный журнал изменений. Это ограничение
следует сообщить пользователям до окна переключения.

## Production cutover

1. Создать администратора, каналы и воронки, оставив polling/webhooks выключенными.
2. Сделать `pg_dump` production.
3. Начать согласованное окно без изменений в amoCRM.
4. Убедиться, что OAuth connection всё ещё `connected`, и выполнить финальный
   импорт `entity_type=all`, `dry_run=false`; при ошибке возобновить с
   сохранённого cursor и текущего `expected_version`.
5. Сверить counts, этапы, ответственных, задачи и контрольную выборку.
6. Включить IMAP polling, Telegram/MAX, generic webhook и HTML-формы.
7. Для каждого канала создать входящее обращение и отправить ответ из сделки.
8. Первые 3–5 дней работать ограниченной группой; amoCRM держать read-only не
   менее 14 дней.

## Стоп-условия

Импорт или переключение останавливается при дублях, расхождении критичных
counts, неверном workspace/ответственном, очереди старше пяти минут, статусе
OAuth connection не `connected` или ошибках расшифровки/обновления token. До
выяснения причины новые страницы импорта не ставятся. Terminal failed jobs
проверяются через `GET /api/v1/admin/jobs?status=failed`; повтор разрешён только
после устранения причины.

При критической проблеме задать `PULSE_JOB_RUNNER_ENABLED=false`, перезапустить
webapp, вернуть webhooks прежней системе и экспортировать записи, созданные в
Pulse после cutover. В `v0.1.0` отдельного встроенного read-only flag нет:
ограничение записи выполняется операционно на входном proxy/maintenance-контуре.
