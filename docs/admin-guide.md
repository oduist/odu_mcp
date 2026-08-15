# Руководство администратора Connect MCP

Документ описывает установку, настройку безопасности, эксплуатацию и
диагностику Connect MCP для Odoo 19.

## Архитектура и границы ответственности

Система состоит из двух компонентов:

```text
MCP client
  | Streamable HTTP + личный Connect MCP API key
  v
FastMCP sidecar, одна реплика
  | тот же Bearer key только в текущем запросе
  v
Odoo module: connect_mcp
  | Odoo user + MCP profile + ACL + record rules + companies
  v
Odoo ORM
```

Odoo отвечает за:

- аутентификацию API-ключа и identity;
- MCP-профили, allowlists и forced domains;
- Odoo ACL, record rules и company context;
- preview, approvals и exactly-once execution;
- audit log и Odoo Bus events.

Sidecar отвечает за:

- MCP Streamable HTTP;
- преобразование MCP tools/resources в control API Odoo;
- HTTP connection pooling и response limits;
- сериализацию операций одного пользователя;
- retry и circuit breaker;
- subject-isolated `subscriptions/listen`.

Sidecar не является источником бизнес-прав и не подключается к PostgreSQL.

## Ограничения текущего релиза

- Поддерживается только Odoo 19.
- Поддерживаются только новые базы.
- Если обнаружена таблица или metadata модели `connect.mcp.credential`, установка
  или upgrade завершаются явной ошибкой.
- Миграция старой credential-схемы намеренно отсутствует.
- Используется ровно одна реплика sidecar.
- Stdio не поддерживается.
- FastMCP зафиксирован на `4.0.0b3`, поэтому обновление зависимости должно
  проходить отдельную protocol regression-проверку.

## Требования

- Odoo `19.0`;
- PostgreSQL 13+, рекомендуется 15 или 16;
- установленный Odoo Bus/evented worker для subscriptions;
- Python 3.11-3.13 или контейнер sidecar;
- HTTPS для Odoo и MCP endpoint;
- reverse proxy с поддержкой WebSocket upgrade;
- отдельный DNS endpoint для sidecar, например `mcp.example.com`.

## Установка Odoo-модуля

Добавьте каталог `addons` репозитория в `addons_path` и установите модуль на
свежей базе:

```bash
odoo \
  --addons-path=/opt/odoo/addons,/opt/connect_addons_ng/addons \
  -d odoo_database \
  -i connect_mcp \
  --stop-after-init
```

Модуль зависит от `base`, `bus`, `mail` и `web`.

Проверьте health endpoint:

```bash
curl --fail https://odoo.example.com/connect_mcp/v1/health
```

## Роли Odoo

Модуль добавляет privilege **Connect MCP** с двумя группами:

| Группа | Возможности |
| --- | --- |
| Auditor | Просмотр профилей, назначений, approvals и audit log |
| Administrator | Настройка профилей и назначений, approval/rejection планов |

MCP Administrator включает права Auditor. Не назначайте эти группы обычным
MCP-пользователям без операционной необходимости.

## Настройка Security Profile

Откройте **Connect MCP > Security Profiles** и создайте профиль по принципу
default deny.

### Общие параметры

| Параметр | Назначение |
| --- | --- |
| Code | Стабильный технический идентификатор профиля |
| Allowed Companies | Пересечение с компаниями пользователя; пусто означает все компании пользователя |
| Max Records Per Call | Верхняя граница записей в одном read-запросе |
| Max Batch Size | Верхняя граница записей в mutation preview |
| Rate Limit Per Minute | Минутный лимит запросов для назначения |
| Daily Quota | Дневной лимит UTC; `0` отключает отдельную квоту |
| Approval TTL | Срок действия preview-плана |

### Feature flags

Отдельно включаются schema discovery, aggregates, reports, attachments,
chatter и activities. `Auto Approve Low Risk` действует только для
низкорисковых collaboration actions и не распространяется на произвольные
create/write/delete.

### Model Policies

Для каждой разрешённой модели создайте одну policy:

1. выберите модель;
2. разрешите только необходимые операции `read/create/write/unlink/aggregate`;
3. укажите точные readable и writable поля;
4. установите per-model record limit при необходимости;
5. задайте `Forced Domain` в JSON-формате.

Пример ограничения партнёров текущей компанией:

```json
[["company_id", "in", [1]]]
```

Forced domain всегда объединяется с domain клиента через `AND`. Пустые списки
полей не означают wildcard: кроме технических `id` и `display_name`, поля
должны быть разрешены явно.

Модели `ir.config_parameter`, `res.users.apikeys` и transient models нельзя
публиковать через policy.

### Allowed Methods

Method policy создавайте только для публичного метода с конкретным business
case. Ограничьте:

- модель и точное имя метода;
- вызов на records или model-level;
- максимальное число records;
- positional arguments;
- точный список разрешённых keyword arguments;
- размер args/kwargs;
- уровень риска.

Не используйте method allowlist как замену специализированному безопасному
tool, если метод имеет сложные или плохо предсказуемые side effects.

## Назначение User Access

Откройте **Connect MCP > User Access** и создайте одно назначение:

- активный внутренний пользователь Odoo;
- один Security Profile;
- понятное имя назначения.

Для одного пользователя допускается ровно одно активное или архивное
назначение. API-ключи в этой записи не создаются и не хранятся.

Пользователь самостоятельно создаёт ключ **MCP only** в своём профиле. Control
API отклоняет unrestricted ключи Odoo, даже если они подходят для обычного RPC.

## Развёртывание sidecar

### Docker

Соберите образ из каталога `addons/connect_mcp/deploy/connect_mcp_server`:

```bash
docker build \
  -t oduist/connect_mcp_server:1.0.0 \
  addons/connect_mcp/deploy/connect_mcp_server
docker push oduist/connect_mcp_server:1.0.0
```

Репозиторий также содержит workflow `Publish Connect MCP server`. При изменениях в
`addons/connect_mcp/deploy/connect_mcp_server/**` он публикует образ
`oduist/connect_mcp_server:sha-<commit>` и дополнительный tag по имени ветки.
Для публикации используются repository secrets `DOCKERHUB_USERNAME` и
`DOCKERHUB_TOKEN`.

Пример запуска:

```bash
docker run -d \
  --name connect-mcp-server \
  --read-only \
  --tmpfs /tmp:size=16m,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  -p 127.0.0.1:8000:8000 \
  -e CONNECT_MCP_ODOO_URL=https://odoo.example.com \
  -e CONNECT_MCP_HOST=0.0.0.0 \
  -e CONNECT_MCP_TOOL_GROUPS=core,write \
  oduist/connect_mcp_server:1.0.0
```

Не запускайте несколько реплик. In-process user locks, watcher registry и
subscription buses намеренно рассчитаны на один процесс.

### Основные переменные

| Переменная | Рекомендуемое значение |
| --- | --- |
| `CONNECT_MCP_ODOO_URL` | Публичный HTTPS URL Odoo для control API |
| `CONNECT_MCP_EVENTS_URL` | Отдельный `ws://` или `wss://` URL evented worker, если основной proxy не маршрутизирует WebSocket |
| `CONNECT_MCP_HOST` | `0.0.0.0` внутри контейнера |
| `CONNECT_MCP_PORT` | `8000` |
| `CONNECT_MCP_MCP_PATH` | `/mcp` |
| `CONNECT_MCP_TOOL_GROUPS` | Минимальный набор, обычно `core`; mutation tools требуют `write` |
| `CONNECT_MCP_VERIFY_TLS` | `true` |
| `CONNECT_MCP_EVENTS_ENABLED` | `true`, если настроен evented routing |

Полный список находится в `addons/connect_mcp/deploy/connect_mcp_server/.env.example`
и `addons/connect_mcp/deploy/connect_mcp_server/README.md`.

## Reverse proxy и WebSocket

Обычные control API endpoints направляйте на HTTP workers Odoo. Маршрут
`/connect_mcp/v1/events` должен попадать на evented/gevent worker так же, как
стандартный `/websocket`.

Если HTTP и evented upstream доступны по разным адресам, оставьте
`CONNECT_MCP_ODOO_URL` на проверяемом HTTPS endpoint и задайте отдельный
`CONNECT_MCP_EVENTS_URL`. Например, внутри доверенной контейнерной сети:

```dotenv
CONNECT_MCP_ODOO_URL=https://odoo.example.com
CONNECT_MCP_EVENTS_URL=ws://odoo:8072/connect_mcp/v1/events
CONNECT_MCP_VERIFY_TLS=true
```

Упрощённая схема Nginx:

```nginx
location ~ ^/(websocket|connect_mcp/v1/events)$ {
    proxy_pass http://odoo_evented;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location / {
    proxy_pass http://odoo_http;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Полный пример единого edge proxy для Odoo UI, MCP endpoint и обоих WebSocket
маршрутов находится в
`addons/connect_mcp/deploy/connect_mcp_server/nginx.edge.example.conf`.

Без `CONNECT_MCP_EVENTS_URL` sidecar строит WebSocket URL из
`CONNECT_MCP_ODOO_URL`, поэтому HTTP и WebSocket должны быть доступны через один
внешний origin. При отдельном evented endpoint используйте явный
`CONNECT_MCP_EVENTS_URL`; для публичного `wss://` сертификат проверяется согласно
`CONNECT_MCP_VERIFY_TLS`.

## Health checks

| Endpoint | Назначение |
| --- | --- |
| Sidecar `/healthz` | Liveness процесса, без обращения к Odoo |
| Sidecar `/readyz` | Проверка доступности Odoo control API |
| Odoo `/connect_mcp/v1/health` | Публичная доступность модуля |

Пример:

```bash
curl --fail https://mcp.example.com/healthz
curl --fail https://mcp.example.com/readyz
curl --fail https://odoo.example.com/connect_mcp/v1/health
```

## Circuit breaker и медленные запросы

Sidecar использует общий transport circuit breaker для доступности Odoo, но
ошибки конкретного пользователя не влияют на остальных.

В failure counter входят только:

- ошибки соединения, чтения, записи, timeout и другие transport errors;
- HTTP `502`, `503`, `504`.

Не входят `401`, `403`, `404`, `409`, `413`, `422`, `429`, policy errors и
response-too-large. После threshold circuit открывается, затем допускает один
half-open probe.

Для одного Odoo subject одновременно выполняется только одна операция. Это
ограничивает занятость Odoo workers при параллельных tool calls, но не отменяет
необходимость правильно настроить Odoo worker/time limits и оптимизировать
дорогие domains, reports и methods.

## Approvals и audit

Approval payload после preview неизменяем. MCP Administrator может одобрить
или отклонить план в **Connect MCP > Approval Inbox**. Execute доступен только
для одобренного, неистёкшего плана и выполняется exactly once.

Audit log содержит request ID, пользователя, профиль, operation, модель,
outcome, status, duration, error и ссылку на approval. Входные данные хешируются
и редактируются; API keys и binary payload не записываются.

Audit и approvals нельзя редактировать вручную. Удаление выполняется только
системным retention cleanup.

## Тестирование

Sidecar:

```bash
cd addons/connect_mcp/deploy/connect_mcp_server
uv sync --extra test
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest --cov=connect_mcp_server --cov-report=term-missing --cov-fail-under=95
```

Odoo-модуль запускайте только на свежей тестовой базе:

```bash
odoo \
  --addons-path=/opt/odoo/addons,/opt/connect_addons_ng/addons \
  -d connect_addons_ng_test \
  -i connect_mcp \
  --test-enable \
  --test-tags=/connect_mcp \
  --stop-after-init
```

Минимальный acceptance test:

1. MCP key получает `200` от `/identity`.
2. Global Odoo key получает `401`.
3. Пользователь не видит модель/поле вне profile allowlist.
4. Forced domain нельзя обойти client domain.
5. Пользователь не может прочитать approval другого пользователя.
6. Preview не меняет данные до approval и execute.
7. Повтор execute не создаёт повторное изменение.
8. Event notification одного subject не доставляется другому.

## Диагностика

| Симптом | Проверка |
| --- | --- |
| Sidecar не стартует | Проверить обязательный `CONNECT_MCP_ODOO_URL` и абсолютные HTTP(S) URL |
| `/readyz` возвращает 503 | Проверить Odoo health, DNS, TLS и reverse proxy |
| Все ключи получают 401 | Проверить установку модуля и что ключ создан как **MCP only** |
| Ключ валиден, но получен 403 | Проверить User Access, active flags, profile и компании |
| Tool отсутствует | Проверить `CONNECT_MCP_TOOL_GROUPS` и перезапустить sidecar |
| Tool есть, но policy denied | Проверить model/field/method policy в Odoo |
| Subscription не приходит | Проверить evented worker и routing `/connect_mcp/v1/events` |
| Circuit остаётся open | Устранить transport/502-504 проблему и дождаться reset interval |

Не включайте debug-логирование HTTP headers на production: Bearer key приходит
в каждом MCP и Odoo control request.

## Обновление и откат

Этот релиз не обновляется поверх старой `connect.mcp.credential` схемы. Для него
нужна свежая база. Не удаляйте old-schema detection для принудительного
upgrade.

Для последующих совместимых версий:

1. сделайте резервную копию базы и filestore;
2. сначала разверните совместимый sidecar image;
3. выполните `-u connect_mcp`;
4. проверьте health и acceptance tests;
5. только после этого переключайте MCP traffic.

## Production security checklist

- [ ] Odoo и MCP доступны только по HTTPS.
- [ ] TLS verification sidecar включена.
- [ ] Sidecar запущен в одной реплике.
- [ ] Пользователи используют личные MCP-only keys.
- [ ] Для каждого пользователя создано только одно User Access assignment.
- [ ] Профили построены по default-deny и не содержат лишних writable fields.
- [ ] Forced domains проверены отдельными negative tests.
- [ ] `unlink` и arbitrary methods разрешены только при документированной необходимости.
- [ ] Odoo administrator не используется как MCP business identity.
- [ ] Reverse proxy не логирует Authorization headers.
- [ ] Настроены health checks, restart policy и централизованные runtime logs.
- [ ] Регулярно проверяются approvals, failures и audit log.
