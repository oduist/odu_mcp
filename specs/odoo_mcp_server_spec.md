# Техническое задание: MCP-сервер `odoo-agent-mcp`

Статус: утверждённая спецификация реализации 1.0

Runtime: Python 3.11–3.13

MCP SDK: стабильная ветка 1.x, exact pin; переход на 2.x только после стабильного релиза и отдельной compatibility-проверки

Лицензия реализации: Apache-2.0

Роль: client-facing MCP protocol plane

## 1. Назначение

Сервер публикует Odoo AI-агентам через Model Context Protocol, преобразуя MCP tools/resources/prompts в защищённые запросы к модулю `odoo_mcp_control`.

Сервер не обходит и не дублирует Odoo security. Все решения о доступе к данным и выполнение изменений остаются внутри Odoo-модуля. Сервер отвечает за:

- MCP lifecycle и transports;
- client authentication;
- tool/resource/prompt schemas;
- нормализацию ошибок;
- выбор task-oriented tool profile;
- approval UX;
- retries только для безопасных/idempotent операций;
- observability и production packaging.

## 2. Принципы

1. Официальный MCP SDK, без собственной реализации протокола.
2. Streamable HTTP — production transport; stdio — локальный transport.
3. Stateless HTTP и JSON responses по умолчанию.
4. Token passthrough в Odoo запрещён: MCP client token и Odoo connector key — разные credentials.
5. Generic read core небольшой; domain packs подключаются конфигурацией.
6. Mutating tools никогда не вызывают прямой write: только preview/approval/execute.
7. Tool output structured, ограничен по объёму и пригоден для следующего вызова.
8. Никаких YOLO/admin режимов.
9. Developer/CI tools не входят в business server process.

## 3. Границы продукта

### Входит

- MCP initialize/ping/tools/resources/prompts;
- stdio и Streamable HTTP;
- health/readiness;
- bearer token resource-server mode;
- optional external OAuth/OIDC issuer с RFC 9728 metadata;
- Odoo connector client с pool, timeouts и safe retries;
- generic ERP tools;
- высокоценные domain summaries;
- profiles/tool groups;
- resources и prompts;
- approval workflow;
- JSON logging, metrics hooks и correlation IDs;
- Dockerfile, sample configuration и tests.

### Не входит

- собственный password login;
- встроенное хранение пользователей;
- выдача Odoo credentials клиенту;
- raw XML-RPC/JSON-2 наружу;
- SQL/Python/shell/module editing;
- production deployment/CI orchestration;
- автоматическое подтверждение high-risk операций.

## 4. Конфигурация

Environment variables с префиксом `ODOO_MCP_`:

- `ODOO_URL`;
- `ODOO_CONNECTOR_TOKEN`;
- `TRANSPORT=stdio|streamable-http`;
- `HOST`, `PORT`, `MCP_PATH`;
- `REQUEST_TIMEOUT_SECONDS`;
- `VERIFY_TLS`;
- `MAX_RESPONSE_BYTES`;
- `TOOL_GROUPS=core,sales,...`;
- `LOG_LEVEL`, `LOG_FORMAT`;
- `AUTH_MODE=none|static-token|oauth`;
- `STATIC_TOKENS` для локального/server-to-server режима;
- `OAUTH_ISSUER_URL`, `RESOURCE_SERVER_URL`, `REQUIRED_SCOPES`;
- `OTEL_*` optional.

Secrets читаются только из environment/secret manager и не входят в diagnostics.

## 5. MCP transports и auth

### Stdio

- Для локального single-user клиента.
- Auth MCP transport не требуется; Odoo connector token остаётся secret process.
- В stdout только MCP frames, логи только stderr.

### Streamable HTTP

- Stateless HTTP, JSON response.
- Default endpoint `/mcp`.
- `/healthz` и `/readyz`.
- В production обязательно client auth.

### OAuth/resource server

- Использовать `TokenVerifier` официального SDK.
- Публиковать RFC 9728 Protected Resource Metadata.
- Валидировать issuer, signature, expiration, audience/resource и scopes.
- Не принимать token, выпущенный только для Odoo или другого resource.
- DCR/authorization UI не реализовывать внутри business MCP: подключать внешний стандартный Authorization Server.

### Static token

- Допустим для controlled server-to-server и development.
- Хранить только SHA-256 digests в конфигурации.
- Constant-time comparison.
- Для HTTP mode `none` запрещён, если host не loopback.

## 6. Odoo connector client

- `httpx.AsyncClient`, keep-alive pool.
- `Authorization: Bearer <connector token>`.
- Уникальный `X-Request-ID` на каждый tool call.
- Timeout connect/read/write/pool.
- Safe retries для health, capabilities и read operations при connect/502/503/504.
- Никогда не retry `changes.preview` или `changes.execute` без idempotency key.
- Ограничить размер входного Odoo response до `MAX_RESPONSE_BYTES`.
- Нормализовать Odoo error envelope в typed exceptions.
- Circuit breaker после повторяющихся transport failures.

## 7. Tool groups

### `core`, включён всегда

- `odoo_server_info`;
- `odoo_whoami`;
- `odoo_list_models`;
- `odoo_describe_model`;
- `odoo_search_records`;
- `odoo_get_record`;
- `odoo_count_records`;
- `odoo_aggregate_records`;
- `odoo_get_change_status`;
- `odoo_execute_approved_change`.

### `write`, выключен по умолчанию

- `odoo_preview_create`;
- `odoo_preview_update`;
- `odoo_preview_delete`;
- `odoo_preview_method`;

Каждый возвращает plan/approval ID, risk, summary, exact target count, redacted diff, expiry и следующий шаг. Выполнение отдельным tool не принимает новый payload — только approval ID.

### `collaboration`

- `odoo_preview_post_message`;
- `odoo_preview_schedule_activity`.

### `documents`

- `odoo_read_attachment`;
- `odoo_preview_upload_attachment`;
- `odoo_render_report`.

### Domain summaries

- `sales`: pipeline/sales snapshot;
- `accounting`: receivables aging summary;
- `inventory`: low-stock/risk snapshot;
- `projects`: project/task status;
- `hr`: absence overview, только если Odoo policy разрешает модели.

Domain tools строятся из generic read/aggregate operations и поэтому автоматически наследуют forced domains и ACL.

## 8. Resources

- `odoo://model/{model}/schema`;
- `odoo://record/{model}/{id}`;
- `odoo://search/{model}?domain=...`;
- `odoo://approval/{approval_id}`;
- `odoo://server/capabilities`.

Resource templates не должны обходить tool policy. Binary content возвращается только explicit attachment resource/tool и с size limit.

## 9. Prompts

- `analyze_records`;
- `summarize_record`;
- `draft_followup`;
- `prepare_change_plan`;
- `investigate_access_denial`;
- `sales_review`;
- `receivables_review`;
- `inventory_risk_review`;
- `project_status_review`.

Prompts не содержат credentials и не обещают агенту недоступные tools. В prompt явно указано: не считать preview выполненным изменением.

## 10. Tool schemas и результаты

- Pydantic/type-hint schemas.
- Model name pattern: `^[a-zA-Z0-9_.]+$`.
- IDs: positive integers, max batch.
- Domain: JSON list, без Python expression.
- Fields/order/groupby проходят локальную базовую проверку; окончательная проверка в Odoo.
- Structured result всегда содержит `request_id`.
- Pagination metadata: offset, limit, returned, has_more.
- Ошибка не маскируется под успешный text result.

## 11. Domain summaries

Domain tools должны:

- сначала проверить наличие/доступность нужной модели;
- корректно работать при отсутствующем Enterprise/optional module;
- учитывать company/currency/timezone из Odoo identity;
- предупреждать о mixed currencies;
- возвращать использованный date range и filters;
- не делать write.

## 12. Approval UX

Preview result:

```json
{
  "approval_id": "uuid",
  "state": "pending",
  "risk_level": "high",
  "summary": "Delete 2 records from crm.lead",
  "target_count": 2,
  "diff": [],
  "expires_at": "...",
  "next_step": "Approve this plan in Odoo, then call odoo_execute_approved_change."
}
```

`odoo_execute_approved_change`:

- принимает только `approval_id`;
- pending/rejected/expired возвращает typed state, не повторяет preview;
- executed возвращает сохранённый результат;
- не имеет `force` параметра.

## 13. Надёжность

- Async I/O, connection pooling.
- Exponential backoff с jitter только для safe reads.
- Circuit breaker states closed/open/half-open.
- Request cancellation передаётся HTTP client.
- Server не кэширует business records.
- Кэш capabilities/schema короткий, ключ включает Odoo profile identity.
- Graceful shutdown закрывает pool.

## 14. Наблюдаемость

- JSON logs в production.
- Поля: timestamp, level, request_id, MCP method/tool, duration, outcome, Odoo error code.
- Redaction Authorization, connector token, binary и values.
- Health не раскрывает URL credentials/database.
- Optional OpenTelemetry traces/metrics hooks.
- Метрики: calls, errors, latency, approval states, connector circuit state.

## 15. Packaging

- `pyproject.toml`, exact MCP SDK pin, lockfile.
- CLI `odoo-agent-mcp`.
- Non-root multi-stage Docker image.
- `.env.example` без секретов.
- `docker-compose.example.yml`.
- README с Claude/Codex/Inspector примерами.
- Configuration validation до запуска.

## 16. Тесты

- Unit: config, schemas, redaction, token verifier, circuit breaker.
- Client: request headers, timeouts, error mapping, response size, safe retry.
- Tools: exact mapping tool → Odoo operation.
- Approval: preview/status/execute and no force.
- Domain packs: missing model, mixed currency, date filters.
- MCP contract: initialize/list tools/call tool/list resources/get resource/list prompts.
- HTTP auth: 401 metadata, invalid audience/scope/token.
- Stdio smoke test.
- Docker health.
- End-to-end с Odoo 19 module.

## 17. Критерии приёмки

1. Сервер стартует в stdio и Streamable HTTP.
2. MCP Inspector успешно выполняет initialize, list и read tool.
3. HTTP без auth в non-loopback production configuration не стартует.
4. Client token никогда не пересылается в Odoo.
5. Read tools соблюдают policy/ACL, подтверждённые Odoo.
6. Mutation невозможно выполнить, передав payload напрямую.
7. Повтор execution безопасен и не создаёт дубликат.
8. Odoo transport error не раскрывает secret/traceback.
9. Unit, MCP contract и end-to-end tests проходят.
10. Документация позволяет развернуть продукт без чтения исходников.
