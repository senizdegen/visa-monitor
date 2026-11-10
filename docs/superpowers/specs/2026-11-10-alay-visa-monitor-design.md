# Alay Visa Monitor — Design Spec

Дата: 2026-11-10
Статус: черновик на согласование

## 1. Цель и рамки

Независимый backend-модуль мониторинга визовых записей. Первый адаптер: Турция → Франция, VFS Global, центр Istanbul, категория Tourism. Система регулярно проверяет источник, хранит историю всех проверок, фиксирует изменения доступности и отдаёт аналитику через REST API и простой dashboard.

Главный критерий заказчика — подключение 50–100 адаптеров без переписывания ядра. Поэтому ядро (очередь, планировщик, хранение, API, аналитика) не знает ничего о конкретных сайтах; адаптер — единственное место, специфичное для источника.

### Вне рамок

- Обход CAPTCHA, Cloudflare, логина и других защит (запрещено ТЗ и условиями VFS).
- Автоматический вход в аккаунт VFS, сохранение пользовательских сессий.
- Уведомления пользователей, авторизация в API, мобильное приложение.
- Celery / RabbitMQ / Redis (обоснование в §4).

## 2. Ограничения источника (наблюдение 2026-11-10)

| URL | Наблюдение |
|---|---|
| `https://visa.vfsglobal.com/tur/en/fra` | SPA (Nuxt): сервер отдаёт пустой каркас `Loading...`, контент рисует JS. За Cloudflare + CloudFront. |
| `https://visa.vfsglobal.com/tur/en/fra/login` | Страница «Waiting Room powered by Cloudflare» с Cloudflare Turnstile (CAPTCHA). |
| CSP сайта | Подключены `challenges.cloudflare.com`, reCAPTCHA, hCaptcha. |
| `https://visa.vfsglobal.com/robots.txt` | Отдаёт HTML SPA, а не robots.txt — явного разрешения на автоматический доступ нет. |
| `https://france-visas.gouv.fr/en/turquie` | Статическая справочная страница; перечисляет центры VFS Istanbul, Ankara, Izmir, Gaziantep. Данных о слотах нет. |

Следствия:

1. Слоты видны только после входа в аккаунт, вход закрыт CAPTCHA. Live-адаптер честно определяет и сохраняет `CAPTCHA_REQUIRED` / `LOGIN_REQUIRED`.
2. Статусы `AVAILABLE`, `NO_SLOTS`, `WAITLIST` демонстрируются демо-адаптером (`source="demo"`) и проверяются тестами на HTML-фикстурах. Фикстуры для страниц за логином — синтетические, составлены по публично известным текстам VFS, и помечены так в тестах и документации.
3. Live-проверки идут с минимальной частотой (по умолчанию раз в 60 минут), без маскировки браузера, и отключаются одной переменной `LIVE_CHECKS_ENABLED=false`.

## 3. Компоненты

```
docker compose
├── postgres   — единственное хранилище и координатор очереди
├── migrate    — разово: alembic upgrade head + синхронизация adapters.yaml → БД
├── api        — FastAPI: REST, Swagger (/docs), dashboard (/dashboard)
└── worker     — asyncio-процесс: планировщик + исполнитель + heartbeat, Playwright Chromium
```

`api` и `worker` — один Python-пакет `alay`, разные точки входа. Между собой общаются только через PostgreSQL. Любой сервис можно запускать в нескольких экземплярах.

### Стек

| Задача | Инструмент |
|---|---|
| Язык | Python 3.12 |
| Пакеты | uv + pyproject.toml |
| API | FastAPI, Pydantic v2, uvicorn |
| БД | PostgreSQL 16, SQLAlchemy 2.0 (async), asyncpg |
| Миграции | Alembic |
| Браузер | Playwright (async API), Chromium |
| Ретраи | tenacity |
| Конфиг | pydantic-settings (env) + `adapters.yaml` (PyYAML) |
| Логи | стандартный `logging`, JSON-формат, поля `job_id`, `adapter_id` |
| Dashboard | Jinja2 + Chart.js (CDN) |
| Тесты | pytest, pytest-asyncio, httpx; реальный PostgreSQL |

## 4. Почему очередь на PostgreSQL, а не Celery

- PostgreSQL уже нужен для хранения; `SELECT … FOR UPDATE SKIP LOCKED` даёт атомарную раздачу задач нескольким worker'ам без брокера.
- Нагрузка: 100 адаптеров × 1 проверка/час ≈ 1.7 задачи/мин — ничтожно для Postgres.
- Меньше сервисов — проще запуск, отладка и демонстрация.
- Переход на брокер имеет смысл при тысячах задач в минуту или нужде в приоритетных очередях; интерфейс адаптера (§6) при этом не меняется — меняется только доставка задач.

## 5. Модель данных

Все времена — `timestamptz` в UTC. Наружу отдаются в часовом поясе адаптера (`Europe/Istanbul` → `+03:00`).

### 5.1 `adapters` — что проверять и когда

| Колонка | Тип | Описание |
|---|---|---|
| `id` | text PK | читаемый ключ, напр. `vfs-tur-fra-istanbul-tourism` |
| `adapter_type` | text | ключ класса в реестре: `vfs_global`, `demo` |
| `source_country` | text | `Turkey` |
| `destination_country` | text | `France` |
| `provider` | text | `VFS Global` |
| `center` | text | `Istanbul` |
| `visa_category` | text | `Tourism` |
| `timezone` | text | `Europe/Istanbul` |
| `config` | jsonb | параметры адаптера (url, маркеры, таймауты) |
| `interval_minutes` | int, > 0 | частота проверок |
| `enabled` | bool | выключение без удаления |
| `next_run_at` | timestamptz | «будильник»: когда создать следующую задачу |
| `created_at`, `updated_at` | timestamptz | |

Индекс: `(next_run_at) WHERE enabled`.

Источник правды — `adapters.yaml`. Сервис `migrate` после `alembic upgrade head` выполняет `python -m alay.sync_adapters` — upsert по `id` (после правки YAML: `docker compose run --rm migrate`): метаданные, `config`, `interval_minutes`, `enabled` обновляются; `next_run_at` у существующих строк не сбрасывается, новым ставится `now()`. Адаптеры, удалённые из YAML, получают `enabled=false` (история сохраняется).

### 5.2 `check_jobs` — очередь задач

| Колонка | Тип | Описание |
|---|---|---|
| `id` | uuid PK | |
| `adapter_id` | text FK → adapters | |
| `trigger` | text | `scheduled` \| `manual` |
| `status` | text | `pending` \| `running` \| `done` \| `failed` |
| `claim_count` | int | сколько раз задачу брал worker (рост при падении worker'а) |
| `locked_by` | text null | id worker'а |
| `locked_until` | timestamptz null | конец аренды |
| `created_at`, `started_at`, `finished_at` | timestamptz | |
| `error` | text null | причина `failed` |

Индексы:
- `UNIQUE (adapter_id) WHERE status IN ('pending','running')` — не больше одной активной задачи на адаптер (защита от дублей на уровне БД).
- `(status, created_at)` — для выборки исполнителем.

Переходы: `pending → running → done`; `running → running` (перезахват после истечения аренды); `running → failed`, если `claim_count` превысил `MAX_CLAIMS` (по умолчанию 3) — в этом случае также пишется `checks` со статусом `ERROR` и `error="worker_lost"`.

`done` означает «результат сохранён», независимо от статуса проверки (в т.ч. `ERROR`).

### 5.3 `checks` — история всех проверок

| Колонка | Тип | Описание |
|---|---|---|
| `id` | bigint identity PK | |
| `job_id` | uuid FK → check_jobs, unique | одна задача — один результат |
| `adapter_id` | text FK → adapters | |
| `checked_at` | timestamptz | момент получения ответа источника |
| `status` | text | см. §6.1 |
| `available_dates` | date[] | непусто только при `AVAILABLE` |
| `slots_count` | int null | число известных слотов; null, если неизвестно |
| `has_waitlist` | bool | |
| `source` | text | `live` \| `demo` |
| `error` | text null | машинный код + текст: `timeout: …`, `http_503`, `access_denied_403` |
| `response_time_ms` | int null | время ответа источника (навигация страницы) |
| `duration_ms` | int | полное время проверки, включая ретраи |
| `attempts` | int | число попыток внутри задачи (ретраи) |
| `evidence_path` | text null | скриншот live-проверки |
| `raw` | jsonb | диагностика: финальный URL, HTTP-код, сработавшее правило |

Индексы: `(adapter_id, checked_at DESC)`, `(checked_at)`, `(status)`.

### 5.4 `slot_events` — изменения доступности

| Колонка | Тип | Описание |
|---|---|---|
| `id` | bigint identity PK | |
| `adapter_id` | text FK | |
| `check_id` | bigint FK → checks | проверка, которая обнаружила изменение |
| `detected_at` | timestamptz | = `checks.checked_at` |
| `event_type` | text | `APPEARED` \| `CHANGED` \| `DISAPPEARED` |
| `dates_before` | date[] | |
| `dates_after` | date[] | |
| `source` | text | `live` \| `demo` |

Индекс: `(adapter_id, detected_at DESC)`.

Правило записи — только для **определённых** результатов:
- `AVAILABLE` → текущие даты = `available_dates`;
- `NO_SLOTS`, `WAITLIST` → текущие даты = `[]`;
- `CAPTCHA_REQUIRED`, `LOGIN_REQUIRED`, `ERROR`, `UNKNOWN` → событие не создаётся и не сравнивается (неизвестность ≠ отсутствие слотов).

Предыдущие даты = `dates_after` последнего события адаптера (или `[]`, если событий не было). Даты сравниваются как отсортированные множества. `[] → непусто` = `APPEARED`; `непусто → []` = `DISAPPEARED`; `непусто → другое непусто` = `CHANGED`; равны — ничего.

### 5.5 `workers` — heartbeat

| Колонка | Тип |
|---|---|
| `id` | text PK (`hostname-pid`) |
| `started_at` | timestamptz |
| `last_seen_at` | timestamptz |

## 6. Адаптеры

### 6.1 `CheckResult` (Pydantic)

Поля как в примере ТЗ: `source_country, destination_country, provider, center, visa_category, checked_at, status, available_dates, slots_count, source, error`, плюс `has_waitlist, response_time_ms, evidence_path, raw`.

Статусы: `AVAILABLE, NO_SLOTS, WAITLIST, LOGIN_REQUIRED, CAPTCHA_REQUIRED, ERROR, UNKNOWN`.

Инварианты (валидатор модели):
- `available_dates` непусто ⇒ `status == AVAILABLE`.
- `status == AVAILABLE` ⇒ `available_dates` непусто **или** `slots_count > 0` (источник подтвердил наличие без дат).
- `status == ERROR` ⇒ `error` задан.
- `status == WAITLIST` ⇒ `has_waitlist == True`.

### 6.2 Интерфейс

```python
class BaseAdapter(ABC):
    def __init__(self, spec: AdapterSpec, ctx: AdapterContext): ...
    @abstractmethod
    async def check(self) -> CheckResult: ...
```

- `AdapterSpec` — строка `adapters` (метаданные + `config`).
- `AdapterContext` — общие ресурсы worker'а: браузер Playwright, логгер, каталог для evidence, `run_number` (число предыдущих проверок этого адаптера; считает сервис проверки).
- Реестр: `@register("vfs_global")` кладёт класс в словарь `ADAPTER_TYPES`. Worker находит класс по `adapter_type`; неизвестный тип → `ERROR` `unknown_adapter_type`.
- Адаптер бросает `TransientSourceError` для временных сбоев (таймаут, сеть, HTTP 5xx) — их ретраит сервис (§7). Всё остальное адаптер возвращает как `CheckResult`.

### 6.3 `VfsGlobalAdapter` (`source="live"`)

Два шага:

1. **fetch** — новый `BrowserContext` (изоляция cookies), `page.goto(config.url)`, ожидание `networkidle` в пределах `config.timeout_s`, затем `PageSnapshot(final_url, http_status, html, response_time_ms)` и скриншот в `evidence/`. Контекст закрывается всегда.
2. **classify(snapshot) → CheckResult** — чистая функция, без сети. Правила по порядку, маркеры берутся из `config` (со значениями по умолчанию):

| # | Условие | Результат |
|---|---|---|
| 1 | маркеры Cloudflare: `challenge-platform`, `cf-turnstile`, `Waiting Room`, `Just a moment` (Cloudflare отдаёт их и с кодами 403/503, поэтому правило первое) | `CAPTCHA_REQUIRED` |
| 2 | HTTP 5xx | `TransientSourceError` |
| 3 | HTTP 429 | `ERROR`, `rate_limited` (без ретрая) |
| 4 | HTTP 403 без маркеров challenge | `ERROR`, `access_denied_403` |
| 5 | `input[type=password]` или `/login` в финальном URL | `LOGIN_REQUIRED` |
| 6 | текст «No appointment slots are currently available» | `NO_SLOTS` |
| 7 | маркер waitlist | `WAITLIST` |
| 8 | «Earliest available slot» + распознаны даты | `AVAILABLE` с датами |
| 9 | иначе | `UNKNOWN` |

Сработавшее правило пишется в `raw.rule`. Браузер запускается без stealth-плагинов и подмены отпечатков.

URL по умолчанию для Istanbul: `https://visa.vfsglobal.com/tur/en/fra/login`.

### 6.4 `DemoAdapter` (`source="demo"`)

- `config.scenario`: список шагов, напр. `[NO_SLOTS, AVAILABLE:+8,+10, AVAILABLE:+10, NO_SLOTS, WAITLIST, CAPTCHA_REQUIRED, ERROR]`; `+8` — дата «сегодня + 8 дней».
- Номер шага = `ctx.run_number` по модулю длины сценария — воспроизводимо и не зависит от перезапусков.
- Шаг `ERROR` бросает `TransientSourceError` (демонстрирует ретраи); шаг `ERROR!` — ошибка без ретрая.
- Небольшая искусственная задержка (`config.delay_ms`), чтобы метрики времени были ненулевые.

### 6.5 Добавление центров и операторов

- Новый центр: блок в `adapters.yaml` с тем же `adapter_type` и другим `center`/`config`.
- Новый оператор: модуль `alay/adapters/<name>.py` с классом `@register("<name>")`. Ядро, схема БД, API и аналитика не меняются.

## 7. Worker

Точка входа: `python -m alay.worker`. Один процесс, `asyncio.TaskGroup` из трёх циклов. Chromium запускается один раз на процесс (только если есть включённые адаптеры, которым он нужен — лениво при первом использовании).

### 7.1 Планировщик (каждые `SCHEDULER_TICK_S`, по умолчанию 5 с)

Одна транзакция:
1. `SELECT id, interval_minutes FROM adapters WHERE enabled AND next_run_at <= now() FOR UPDATE SKIP LOCKED`.
2. Для каждого: `INSERT INTO check_jobs (…, trigger='scheduled', status='pending') ON CONFLICT DO NOTHING` (частичный уникальный индекс).
3. `UPDATE adapters SET next_run_at = now() + interval_minutes * interval '1 minute'`.

Если worker был выключен несколько часов, при старте создаётся одна задача на адаптер, а не пропущенные «догоняющие».

### 7.2 Исполнитель

- Ёмкость: `asyncio.Semaphore(WORKER_CONCURRENCY)`, по умолчанию 3.
- Захват (короткая транзакция, коммит сразу):
  ```sql
  UPDATE check_jobs SET status='running', locked_by=:me,
         locked_until = now() + :lease, started_at = coalesce(started_at, now()),
         claim_count = claim_count + 1
  WHERE id IN (SELECT id FROM check_jobs
               WHERE status='pending' OR (status='running' AND locked_until < now())
               ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT :free)
  RETURNING *;
  ```
- Аренда `JOB_LEASE_S` = 180 с; должна быть больше `CHECK_TIMEOUT_S` (90 с) × попытки — проверка ограничена общим таймаутом 150 с.
- Задача с `claim_count > MAX_CLAIMS` → `failed` + `checks` `ERROR worker_lost`.
- Если исполнитель ничего не взял — пауза `EXECUTOR_IDLE_S` (1 с).

### 7.3 Сервис проверки (`services/checks.py`, общий для worker'а и тестов)

1. Создать адаптер из реестра.
2. `tenacity`: повтор только на `TransientSourceError`, до `RETRY_ATTEMPTS` (3), экспоненциально 2 → 4 → 8 с; каждая попытка внутри `asyncio.timeout(CHECK_TIMEOUT_S)`.
3. Исчерпаны попытки → `CheckResult(status=ERROR, error=<последняя ошибка>)`. Неожиданное исключение адаптера → `ERROR` `adapter_crash: …` + лог с traceback.
4. Одна транзакция: `INSERT checks` → вычислить и при необходимости `INSERT slot_events` (§5.4) → `check_jobs.status='done'`, `finished_at`. Если `locked_by` уже не наш (аренду перехватили) — откат, лог предупреждения.

### 7.4 Heartbeat

Каждые 15 с upsert в `workers`. Graceful shutdown: SIGTERM/SIGINT → событие `stop`; циклы перестают брать задачи, текущие дорабатывают (до `SHUTDOWN_GRACE_S` = 60 с), браузер закрывается, строка worker'а удаляется.

## 8. API

Swagger: `/docs`. Ответы — Pydantic-модели. Времена — ISO 8601 с поясом адаптера.

| Метод и путь | Описание |
|---|---|
| `GET /adapters` | список адаптеров: метаданные, `enabled`, `interval_minutes`, `next_run_at`, последний статус |
| `POST /adapters/{id}/check` | создаёт `manual`-задачу. `202 {job_id, status, deduplicated}`; при существующей активной задаче — её же с `deduplicated=true`. `404` — нет адаптера, `409` — адаптер выключен или это live-адаптер при `LIVE_CHECKS_ENABLED=false`. `?wait=true` — ждать результата до 60 с (опрос БД раз в 0.5 с), затем `200` с результатом или `202` |
| `GET /jobs/{id}` | статус задачи + результат (если есть) |
| `GET /checks` | история. Фильтры: `adapter_id, status, source, since, until`; `limit` (≤ 500, по умолч. 50), `offset`; сортировка по `checked_at DESC` |
| `GET /availability` | последний результат по каждому включённому адаптеру; фильтр `source` |
| `GET /slot-events` | история изменений доступности; фильтры как у `/checks` |
| `GET /analytics` | агрегаты (§9); `source` (по умолч. `live`), `adapter_id` (опц.) |
| `GET /health` | `ok` / `degraded` / `down`: БД доступна; есть worker с heartbeat < 60 с; нет задач `pending` старше 5 мин |
| `GET /dashboard` | HTML-страница аналитики |

## 9. Аналитика

Считается SQL-запросами по `checks` и `slot_events`, фильтр `source` обязателен (по умолчанию `live`). Ответ `GET /analytics`:

```json
{
  "source": "live",
  "generated_at": "…",
  "periods": {
    "24h": { "checks_total": 24, "by_status": {"CAPTCHA_REQUIRED": 23, "ERROR": 1},
             "technical_success_rate": 0.958, "errors": 1,
             "avg_response_time_ms": 2100, "p95_response_time_ms": 3400,
             "avg_duration_ms": 4200, "available_observations": 0,
             "slot_events": 0, "insufficient_data": false },
    "7d":  { … },
    "30d": { … }
  },
  "timeline": [ {"bucket": "2026-11-10T10:00+03:00", "checks": 1, "available": 0, "errors": 0}, … ],
  "limitations": ["За 24ч 23 из 24 проверок остановлены CAPTCHA: данные о слотах недоступны."]
}
```

Определения:
- **Техническая успешность** — доля проверок со статусом не `ERROR` и не `UNKNOWN`. `CAPTCHA_REQUIRED`/`LOGIN_REQUIRED` — технический успех (страница распознана), но не наличие слотов.
- **available_observations** — число проверок со статусом `AVAILABLE`.
- **timeline** — почасовые корзины за 24ч или подневные за 7д/30д (`date_trunc`), параметр `bucket=hour|day`.
- **insufficient_data** — `true`, если в периоде меньше `MIN_CHECKS_FOR_STATS` (по умолчанию 5) проверок; тогда проценты = `null`.
- **limitations** — генерируется правилами: доля `CAPTCHA_REQUIRED`/`LOGIN_REQUIRED` > 50% → пояснение; нет данных → пояснение; `source=demo` → «демонстрационные данные».

### Dashboard

Одна страница (Jinja2 + Chart.js), данные из `/analytics`, `/checks`, `/slot-events`:
- переключатель Live / Demo; в режиме Demo — постоянный баннер «Демонстрационные данные»;
- карточки 24ч/7д/30д: проверки, успешность, ошибки, среднее время;
- график по времени: проверки по статусам (stacked bar);
- таблица последних проверок и таблица `slot_events`;
- блок «Ограничения источника» из `limitations`.

## 10. Конфигурация (env)

| Переменная | По умолч. |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://alay:alay@postgres:5432/alay` |
| `ADAPTERS_FILE` | `adapters.yaml` |
| `LIVE_CHECKS_ENABLED` | `true` (false — live-адаптеры не планируются) |
| `DEFAULT_INTERVAL_MINUTES` | `60` |
| `SCHEDULER_TICK_S` | `5` |
| `WORKER_CONCURRENCY` | `3` |
| `CHECK_TIMEOUT_S` / `CHECK_TOTAL_TIMEOUT_S` | `90` / `150` |
| `RETRY_ATTEMPTS` | `3` |
| `JOB_LEASE_S` / `MAX_CLAIMS` | `180` / `3` |
| `EVIDENCE_DIR` | `/data/evidence` |
| `LOG_LEVEL` | `INFO` |

`adapters.yaml` по умолчанию: `vfs-tur-fra-istanbul-tourism` (live, 60 мин) и `demo-tur-fra-istanbul-tourism` (demo, 1 мин). Закомментированные примеры для Ankara, Izmir, Gaziantep.

## 11. Обработка ошибок — сводка

| Ситуация | Поведение |
|---|---|
| Таймаут / сеть / 5xx | ретрай 3 раза, затем `ERROR` |
| 429 | `ERROR rate_limited`, без ретрая |
| CAPTCHA / логин | соответствующий статус, без ретрая |
| Неожиданное исключение адаптера | `ERROR adapter_crash`, traceback в лог, worker продолжает |
| Падение worker'а | аренда истекает, задачу берёт другой worker; после `MAX_CLAIMS` — `failed` |
| БД недоступна | циклы логируют и повторяют с паузой; `/health` → `down` |
| Неизвестный `adapter_type` | `ERROR unknown_adapter_type` |

## 12. Структура репозитория

```
alay/
├── docker-compose.yml, Dockerfile, pyproject.toml, uv.lock
├── adapters.yaml, .env.example, alembic.ini
├── README.md, LIMITATIONS.md
├── migrations/
├── src/alay/
│   ├── config.py            # pydantic-settings
│   ├── logging.py           # JSON-логи
│   ├── domain.py            # CheckStatus, CheckResult, AdapterSpec
│   ├── adapters/            # base.py, registry.py, vfs_global.py, vfs_classifier.py, demo.py
│   ├── db/                  # models.py, session.py, repositories/
│   ├── services/            # checks.py, slot_events.py, analytics.py, adapters_sync.py
│   ├── worker/              # main.py, scheduler.py, executor.py, browser.py, heartbeat.py
│   └── api/                 # main.py, routes/, schemas.py, templates/dashboard.html
├── tests/
│   ├── fixtures/vfs/*.html  # captcha, login, no_slots*, waitlist*, available* (* синтетические)
│   ├── unit/
│   └── integration/
└── docs/
```

## 13. Тестирование

pytest + pytest-asyncio; интеграционные тесты на реальном PostgreSQL (`TEST_DATABASE_URL`, в compose — `docker compose run --rm tests`). Сеть в тестах не используется.

- **classify**: каждый статус на фикстурах; маркеры из `config` переопределяются.
- **CheckResult**: инварианты §6.1.
- **DemoAdapter**: воспроизводимая последовательность.
- **Очередь**: два конкурентных захвата не получают одну задачу; просроченная аренда перехватывается; `MAX_CLAIMS` → `failed`; дубль активной задачи не создаётся; планировщик сдвигает `next_run_at`.
- **Сервис проверки**: ретраи на `TransientSourceError` (`attempts=3`); без ретраев на CAPTCHA; падение адаптера → `ERROR`.
- **slot_events**: APPEARED / CHANGED / DISAPPEARED; повтор без события; CAPTCHA/ERROR не влияют.
- **API**: все endpoints через `httpx.AsyncClient`, включая дедупликацию ручной проверки и `?wait=true`.
- **Аналитика**: на заранее заполненных данных — точные цифры, `insufficient_data`, разделение live/demo.
- **Ручная проверка live**: один прогон против VFS вне тестов, результат и скриншот — в README.

## 14. Документация и демонстрация

- `README.md`: запуск (`docker compose up`), сценарий демо, примеры ответов API, добавление центра и оператора, масштабирование.
- `LIMITATIONS.md`: наблюдения §2, что не делаем и почему, шаги интеграции: официальный API или партнёрство с VFS, оператор с ручной сессией, уведомления по `slot_events`, переход на брокер при росте.

Сценарий созвона: `docker compose up` → `/docs` → `POST /adapters/vfs-…/check?wait=true` (live, `CAPTCHA_REQUIRED` + скриншот) → то же для demo (`AVAILABLE`) → `/checks`, `/slot-events` → `/dashboard` Live/Demo → остановка worker'а (`/health` = degraded) → добавление Ankara в YAML.
