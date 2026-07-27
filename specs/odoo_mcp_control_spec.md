# Техническое задание: Odoo-модуль `odoo_mcp_control`

Статус: утверждённая спецификация реализации 1.0

Целевая платформа: Odoo 19.0 Community и Enterprise

Лицензия реализации: LGPL-3

Роль: защищённый control/data plane внутри Odoo

## 1. Назначение

Модуль предоставляет внешнему MCP-серверу минимальный, стабильный и контролируемый API к Odoo. Он не реализует MCP transport и OAuth клиентского уровня. Его ответственность:

- идентифицировать connector credential и связанного Odoo-пользователя;
- применить политики profile → model → operation → field → forced domain;
- обязательно применить штатные Odoo ACL, field groups, record rules и multi-company context;
- безопасно выполнять чтение и агрегирование;
- проводить любые изменения через неизменяемый план, approval и однократное выполнение;
- вести коррелированный аудит от входного запроса до изменённых записей;
- предоставить администратору понятный backend UI.

## 2. Принципы

1. Default deny: отсутствие policy означает запрет.
2. Никакого `sudo()` при чтении или изменении бизнес-данных.
3. Credential всегда привязан к активному Odoo user.
4. Forced domain добавляется сервером и не может быть ослаблен клиентом.
5. Поля разрешаются отдельными read/write allowlists.
6. Любая мутация по умолчанию проходит `preview → human approval → execute`.
7. Approval привязан к каноническому payload hash, credential, сроку и точному набору записей.
8. Один approval выполняется не более одного раза.
9. Произвольные private methods, SQL, Python-код и файловые операции запрещены.
10. Логи не хранят secrets, содержимое бинарных файлов и полные чувствительные values.

## 3. Границы продукта

### Входит

- REST/JSON connector API `/odoo_mcp/v1/*`;
- credentials, profiles, model/method policies;
- read/search/count/aggregate/schema;
- вложения и отчёты при отдельном разрешении;
- change plans для create/update/delete/message/activity/attachment/method;
- approval UI;
- rate limits, quotas, expiration и IP allowlist;
- audit, health, capabilities и cleanup cron;
- Odoo 19 tests.

### Не входит

- MCP protocol lifecycle;
- client-facing OAuth Authorization Server;
- LLM provider или chatbot;
- raw SQL, arbitrary Python, shell, addon editing;
- автоматическое расширение policy при установке новых Odoo-модулей;
- обход ACL/record rules;
- хранение plaintext connector secret после выдачи.

## 4. Пользовательские роли

- `MCP Auditor`: только чтение profiles, policies, approvals и audit.
- `MCP Manager`: настройка profiles/policies, выпуск/revoke credentials, approve/reject plans, cleanup.
- Connector service user: обычный Odoo-пользователь с минимальными business groups. Backend-доступ к MCP configuration ему не требуется.

## 5. Модель данных

### `odoo.mcp.profile`

- `name`, `active`;
- `code`, уникальный стабильный идентификатор;
- `description`;
- `company_ids`;
- `max_records_per_call` — 1…1000, default 100;
- `max_batch_size` — 1…100, default 20;
- `rate_limit_per_minute` — default 60;
- `daily_quota` — 0 означает без отдельной квоты;
- `approval_ttl_minutes` — default 30;
- `auto_approve_low_risk` — default false;
- feature flags: schema, aggregate, reports, attachments, chatter, activities;
- `policy_ids`, `method_policy_ids`, `credential_ids`.

### `odoo.mcp.model.policy`

- `profile_id`, `model_id`, уникальная пара;
- operation flags: read, create, write, unlink, aggregate;
- `read_field_ids`, `write_field_ids`;
- `forced_domain_json`, только JSON-массив Odoo domain;
- optional per-model `max_records`;
- binary read/write flags;
- запрет transient/abstract models.

### `odoo.mcp.method.policy`

- `profile_id`, `model_id`, `method_name`, уникальная тройка;
- `active`;
- `risk_level`: low/medium/high/critical;
- `max_record_count`;
- model-level calls disabled by default;
- positional arguments disabled by default;
- exact allowlist of keyword arguments and canonical JSON size limit;
- public method only; имя с `_` запрещено;
- все method calls проходят общий approval workflow.

### `odoo.mcp.credential`

- `name`, `active`;
- `profile_id`, `user_id`;
- `key_prefix`, `secret_digest`, `secret_hint`;
- `expires_at`, `last_used_at`;
- `allowed_ip_networks`;
- `request_count`, `failure_count`;
- `revoked_at`;
- метод выпуска возвращает plaintext secret ровно один раз.

Формат ключа: `omcp_<public-prefix>_<random-secret>`. В БД хранится SHA-256 полного высокоэнтропийного ключа. Сравнение выполняется constant-time.

### `odoo.mcp.approval`

- UUID `request_uid`;
- credential/profile/user;
- `action`, `model_name`;
- канонический `payload_json`, `payload_hash`;
- `risk_level`, `summary`, `target_count`, `diff_json`;
- state: pending/approved/rejected/executing/executed/expired/failed;
- requested/approved/rejected/executed timestamps и users;
- `expires_at`;
- `result_json`, `error_message`;
- unique idempotency key в пределах credential.

### `odoo.mcp.audit.log`

- `request_id`, credential/profile/user;
- operation/model;
- input hash и безопасная сводка;
- target IDs в JSON с установленным лимитом;
- outcome, HTTP/error code, error class;
- duration, remote IP, user agent;
- approval reference;
- immutable после создания, кроме системного retention cleanup.

## 6. Connector API

Все JSON endpoints, кроме health, требуют:

```http
Authorization: Bearer omcp_...
Content-Type: application/json
X-Request-ID: UUID
```

Максимальный body по умолчанию: 2 MiB. Для каждого ответа:

```json
{
  "ok": true,
  "request_id": "...",
  "data": {},
  "meta": {
    "duration_ms": 12,
    "profile": "analyst"
  }
}
```

Ошибка:

```json
{
  "ok": false,
  "request_id": "...",
  "error": {
    "code": "policy_denied",
    "message": "Operation is not allowed",
    "retryable": false
  }
}
```

В production response не содержит traceback, SQL, filesystem paths или credential details.

### Endpoints

- `GET /odoo_mcp/v1/health`: liveness без секретов и DB metadata.
- `GET /odoo_mcp/v1/capabilities`: доступные операции текущего profile.
- `POST /odoo_mcp/v1/execute`: единая типизированная операция.

### Read operations

- `system.info`;
- `identity.whoami`;
- `models.list`;
- `models.describe`;
- `records.search`;
- `records.read`;
- `records.count`;
- `records.aggregate`;
- `attachments.read`;
- `reports.render`;
- `changes.status`.

### Mutating actions, только через change plan

- `record.create`;
- `record.update`;
- `record.delete`;
- `message.post`;
- `activity.schedule`;
- `attachment.create`;
- `method.call`.

### Workflow

1. `changes.preview` принимает action payload.
2. Модуль проверяет credential, policy, ACL/record rules, поля, forced domain, batch limits.
3. Модуль вычисляет target set и redacted diff.
4. Создаётся approval.
5. Менеджер approve/reject в Odoo, либо low-risk plan auto-approved разрешённым profile.
6. `changes.execute` блокирует approval row, повторяет критические проверки и выполняет payload в savepoint.
7. Повторный execute возвращает сохранённый result без повторной мутации.

## 7. Правила операций

### Search/read

- Клиентский domain должен быть валидным JSON Odoo domain.
- Итоговый domain равен `forced_domain AND client_domain`.
- `limit` ограничивается profile и model policy.
- Offset имеет верхний предел 100000.
- Order допускает только реальные доступные поля и `asc|desc`.
- Поля пересекаются с field-group access и policy allowlist.
- Binary fields исключаются, пока не разрешены явно.

### Aggregate

- Только при `allow_aggregate`.
- Group-by и aggregate fields должны быть readable и stored.
- Разрешены штатные агрегаторы Odoo; raw SQL expression запрещён.
- Результат ограничен числом групп.

### Create/update

- Только write-field allowlist.
- Запрещены magic/system fields и неизвестные поля.
- Odoo create/write access проверяется обычным user environment.
- Multi-company context ограничен пересечением profile companies и user companies.
- После create/write forced domain проверяется повторно; выход записи из scope
  откатывает всю mutation.

### Delete

- Всегда risk `high`.
- Exact IDs фиксируются при preview.
- Перед execute повторно проверяется forced domain и доступ.

### Method call

- Только exact allowlist в `odoo.mcp.method.policy`.
- Private methods запрещены.
- Не допускаются callable names из payload, отличные от policy.
- Результат проходит ограниченную JSON-сериализацию.

### Attachments/reports

- Размер upload/download и PDF ограничивается системной настройкой.
- Attachment должен быть связан с record, разрешённым model policy.
- Report model должен совпадать с разрешённой моделью и exact IDs.

## 8. Безопасность

- HTTPS обязателен на reverse proxy; опциональный режим разработки по HTTP не меняет auth.
- Secrets никогда не логируются и не отображаются повторно.
- IP allowlist поддерживает IPv4/IPv6 CIDR.
- Rate limit: rolling minute по credential; daily quota по UTC.
- Revoked/expired/inactive credential отклоняется до ORM.
- `request.env(user=credential.user_id)` используется для business data; configuration читается через ограниченный `sudo`.
- Запрещено автоматически добавлять admin/system models.
- Ошибки AccessError/ValidationError/UserError переводятся в стабильные error codes.
- Все mutation handlers исполняются внутри savepoint.
- Audit создаётся и при неуспешной операции.
- Cleanup cron удаляет только истёкшие approvals и audit старше retention.

## 9. Backend UI

Раздел `Settings → Technical → MCP Control`:

- Profiles;
- Model policies;
- Method policies;
- Credentials;
- Approval inbox;
- Audit log.

UI должен:

- показывать предупреждение, если profile не имеет policies;
- показывать secret только в transient wizard после выпуска;
- иметь approve/reject actions с confirmation;
- запрещать редактирование payload approval;
- давать smart buttons profile → policies/credentials/audit;
- иметь фильтры pending approvals, failed requests, revoked credentials.

## 10. Нефункциональные требования

- Odoo 19 coding conventions и новый `Domain` API.
- Никаких внешних Python dependencies.
- Средняя overhead policy+audit без ORM operation: менее 50 ms на типичной PostgreSQL.
- Один read response не более 10 MiB, по умолчанию не более 100 records.
- Структурированное логирование с request ID.
- Translatable UI strings.
- Совместимость с несколькими workers.

## 11. Тесты

Обязательные Odoo tests:

- valid/invalid/revoked/expired credential;
- CIDR allowlist;
- profile/model/operation/field deny;
- forced domain невозможно обойти;
- ACL, record rules, field groups и multi-company;
- max limit/batch/rate/daily quota;
- preview exact diff;
- approve/reject/expire;
- execute exactly once при повторе;
- concurrent execute lock;
- method allowlist/private method deny;
- attachment/report size;
- audit success/failure и redaction;
- immutable audit;
- JSON error stability.

## 12. Критерии приёмки

1. Модуль устанавливается и обновляется на чистой Odoo 19.
2. Без profile policy невозможно прочитать или изменить business model.
3. Odoo user, не имеющий доступа к записи, не получает её через connector.
4. Ни одна write-операция default profile не выполняется без approval.
5. Повтор `changes.execute` не создаёт повторных изменений.
6. Credential secret отсутствует в логах и базе в plaintext.
7. Все тесты проходят без warnings уровня ERROR/CRITICAL.
8. Внешний MCP-сервер проходит end-to-end сценарии по опубликованному API.
