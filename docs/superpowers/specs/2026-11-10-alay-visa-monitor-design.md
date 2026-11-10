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
2. Статусы `AVAILABLE`, `NO_SLOTS`, `WAITLIST` демонстрируются на локальном учебном сайте `mock-vfs` (§6.4): тот же `VfsGlobalAdapter` проходит на нём весь путь браузером — вход, выбор центра и категории, чтение ответа. Такие адаптеры имеют `source="demo"`. Страницы `mock-vfs` за «логином» — синтетические, составлены по публично известным текстам VFS, и помечены так в коде и документации.
3. Live-проверки идут с минимальной частотой (по умолчанию раз в 60 минут), без маскировки браузера, и отключаются одной переменной `LIVE_CHECKS_ENABLED=false`.

## 3. Компоненты

```
docker compose
├── postgres   — единственное хранилище и координатор очереди
├── migrate    — разово: alembic upgrade head + синхронизация adapters.yaml → БД
├── api        — FastAPI: REST, Swagger (/docs), dashboard (/dashboard)
├── worker     — asyncio-процесс: планировщик + исполнитель + heartbeat, Playwright Chromium
└── mock-vfs   — учебный сайт, имитирующий путь записи VFS, для воспроизводимых демо (§6.4)
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
| `adapter_type` | text | ключ класса в реестре: `vfs_global` |
| `source` | text | `live` (реальный источник) \| `demo` (mock-vfs); копируется в каждый результат |
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
- `AdapterContext` — общие ресурсы worker'а: браузер Playwright, логгер, каталог для evidence.
- Реестр: `@register("vfs_global")` кладёт класс в словарь `ADAPTER_TYPES`. Worker находит класс по `adapter_type`; неизвестный тип → `ERROR` `unknown_adapter_type`.
- Адаптер бросает `TransientSourceError` для временных сбоев (таймаут, сеть, HTTP 5xx) — их ретраит сервис (§7). Всё остальное адаптер возвращает как `CheckResult`.

### 6.3 `VfsGlobalAdapter`

Один класс для реального VFS (`source="live"`) и для `mock-vfs` (`source="demo"`); различаются только `config`. Адаптер повторяет путь человека и после каждого шага смотрит, не упёрся ли он в стену.

**Сценарий (`navigate`)** — в новом `BrowserContext` (изоляция cookies), каждое действие с таймаутом `config.timeout_s`:

| Шаг | Действие | Если не вышло |
|---|---|---|
| 1 | `page.goto(config.start_url)`, замер `response_time_ms` | classify → обычно `CAPTCHA_REQUIRED` / `ERROR` |
| 2 | Страница входа? Если `config.login` не задан → `LOGIN_REQUIRED`. Иначе заполнить email/пароль, нажать «Sign In» | classify |
| 3 | Нажать «Start New Booking» | classify → `UNKNOWN` |
| 4 | Выбрать центр (`config.center_label`), категорию, подкатегорию в выпадающих списках по подписи поля | classify → `UNKNOWN` |
| 5 | Дождаться блока с ответом о слотах (`config.selectors.result`) | `UNKNOWN` |
| 6 | classify финальной страницы | — |

После остановки делается `PageSnapshot(final_url, http_status, html, step)` и скриншот в `evidence/`. Контекст закрывается всегда. Номер шага, на котором остановились, пишется в `raw.step`.

**Защита от автоматического входа на реальный сайт.** Шаг 2 с заполнением пароля выполняется, только если хост `start_url` есть в `LOGIN_ALLOWED_HOSTS` (по умолчанию `mock-vfs,localhost`). Для `vfsglobal.com` адаптер возвращает `LOGIN_REQUIRED` даже при заданных учётных данных.

Селекторы находятся по видимым подписям (`get_by_role`, `get_by_label`, `get_by_text`), а тексты — в `config.selectors`, со значениями по умолчанию под VFS.

**classify(snapshot) → CheckResult** — чистая функция, без сети. Правила по порядку, маркеры берутся из `config` (со значениями по умолчанию):

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

`start_url` по умолчанию для Istanbul: `https://visa.vfsglobal.com/tur/en/fra/login`.

### 6.4 `mock-vfs` — учебный сайт для демонстраций

Небольшое FastAPI-приложение в том же репозитории (`alay.mock_vfs`), отдельный сервис в compose (порт 8081). На каждой странице баннер «Mock VFS — тестовый сайт, не связан с VFS Global». Логотипов и фирменного стиля VFS нет.

Страницы повторяют путь записи:
- `/{scenario}/login` — форма входа (демо-логин из `.env.example`); контент дорисовывается JavaScript через ~0.5 с, как на настоящем SPA;
- `/{scenario}/dashboard` — кнопка «Start New Booking»;
- `/{scenario}/booking` — три выпадающих списка (центр, категория, подкатегория); после выбора JS запрашивает `/api/slots` и показывает блок с ответом.

Сценарии (`{scenario}` в URL):

| Сценарий | Что показывает сайт | Ожидаемый статус |
|---|---|---|
| `available` | «Earliest available slot … : DD-MM-YYYY» + даты (сегодня +8 и +10 дней) | `AVAILABLE` |
| `no-slots` | «No appointment slots are currently available» | `NO_SLOTS` |
| `waitlist` | сообщение о листе ожидания | `WAITLIST` |
| `captcha` | страница «Waiting Room» с маркерами Turnstile | `CAPTCHA_REQUIRED` |
| `login-wall` | форма входа, демо-логин не задан в адаптере | `LOGIN_REQUIRED` |
| `server-error` | HTTP 503 | ретраи → `ERROR` |
| `slow` | ответ через 120 с | таймаут → ретраи → `ERROR` |
| `changed-layout` | кнопки переименованы | `UNKNOWN` |
| `rotate` | каждую минуту следующий из сценариев выше по кругу (по номеру минуты) | меняется |

В `adapters.yaml` по умолчанию: `mock-istanbul-tourism` на `rotate` с интервалом 1 минута (копит историю для dashboard и даёт `slot_events`). Остальные сценарии используются для ручной проверки на созвоне и в тестах.

Страницы `mock-vfs` — это синтетика по публично известным текстам VFS; они проверяют, что сценарий браузера и classify работают, но не доказывают, что реальный VFS выглядит так же.

### 6.5 Добавление центров и операторов

- Новый центр: блок в `adapters.yaml` с тем же `adapter_type` и другим `center`/`config`.
- Новый оператор: модуль `alay/adapters/<name>.py` с классом `@register("<name>")`. Ядро, схема БД, API и аналитика не меняются.

## 7. Worker

Точка входа: `python -m alay.worker`. Один процесс, `asyncio.TaskGroup` из трёх циклов. Chromium запускается один раз на процесс (только если есть включённые адаптеры, которым он нужен — лениво при первом использовании).

### 7.1 Планировщик (каждые `SCHEDULER_TICK_S`, по умолчанию 5 с)

Одна транзакция:
1. `SELECT id, interval_minutes FROM adapters WHERE enabled AND next_run_at <= now() FOR NO KEY UPDATE SKIP LOCKED`. Именно `NO KEY UPDATE`, а не `FOR UPDATE`: вставка в `check_jobs` проверяет внешний ключ через `FOR KEY SHARE` на строке адаптера, и с `FOR UPDATE` параллельная ручная проверка ловит deadlock.
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
               WHERE status='pending'
                  OR (status='running' AND locked_until < now() AND claim_count < :max_claims)
               ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT :free)
  RETURNING *;
  ```
- Аренда `JOB_LEASE_S` = 180 с; должна быть больше `CHECK_TIMEOUT_S` (90 с) × попытки — проверка ограничена общим таймаутом 150 с.
- Задача, у которой аренда истекла после `MAX_CLAIMS` захватов (`claim_count >= MAX_CLAIMS`), → `failed` + `checks` `ERROR worker_lost`.
- Если исполнитель ничего не взял — пауза `EXECUTOR_IDLE_S` (1 с).

### 7.3 Сервис проверки (`services/checks.py`, общий для worker'а и тестов)

1. Создать адаптер из реестра.
2. `tenacity`: повтор только на `TransientSourceError`, до `RETRY_ATTEMPTS` (3 попытки), паузы между ними экспоненциальные: 2 с, затем 4 с (`RETRY_WAIT_BASE_S` = 2); каждая попытка внутри `asyncio.timeout(CHECK_TIMEOUT_S)`.
3. Исчерпаны попытки → `CheckResult(status=ERROR, error=<последняя ошибка>)`. Неожиданное исключение адаптера → `ERROR` `adapter_crash: …` + лог с traceback.
4. Одна транзакция: `INSERT checks` → вычислить и при необходимости `INSERT slot_events` (§5.4) → `check_jobs.status='done'`, `finished_at`. Если `locked_by` уже не наш (аренду перехватили) — откат, лог предупреждения.

### 7.4 Heartbeat

Каждые 15 с upsert в `workers`. Graceful shutdown: SIGTERM/SIGINT → событие `stop`; циклы перестают брать задачи, текущие дорабатывают (до `SHUTDOWN_GRACE_S` = 60 с), браузер закрывается, строка worker'а удаляется.

## 8. API

Swagger: `/docs`. Ответы — Pydantic-модели. Времена — ISO 8601 с поясом адаптера.

| Метод и путь | Описание |
|---|---|
| `GET /adapters` | список адаптеров: метаданные, `enabled`, `interval_minutes`, `next_run_at`, последний статус |
| `POST /adapters/{id}/check` | создаёт `manual`-задачу. `202 {job_id, status, deduplicated}`; при существующей активной задаче — её же с `deduplicated=true`. `404` — нет адаптера, `409` — live-адаптер при `LIVE_CHECKS_ENABLED=false`. `enabled=false` выключает только расписание; вручную такой адаптер запустить можно. `?wait=true` — ждать результата до 60 с (опрос БД раз в 0.5 с), затем `200` с результатом или `202` |
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
- **limitations** — генерируется правилами: доля проверок, заблокированных источником (`CAPTCHA_REQUIRED`, `LOGIN_REQUIRED`, `ERROR` с `access_denied_403` или `rate_limited`), > 50% за 24ч → пояснение; нет данных → пояснение; `source=demo` → «демонстрационные данные».

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
| `LOGIN_ALLOWED_HOSTS` | `mock-vfs,localhost` |
| `MOCK_VFS_EMAIL` / `MOCK_VFS_PASSWORD` | демо-логин для mock-vfs |
| `HEADFUL` | `false` (true — окно браузера видно; только при локальном запуске worker'а) |
| `LOG_LEVEL` | `INFO` |

`adapters.yaml` по умолчанию:
- `vfs-tur-fra-istanbul-tourism` — live, `https://visa.vfsglobal.com/tur/en/fra/login`, 60 мин;
- `mock-istanbul-tourism` — demo, `http://mock-vfs:8081/rotate/login`, 1 мин;
- `mock-<scenario>` — demo, по одному на сценарий §6.4, `enabled: false` (вне расписания, только ручной запуск);
- закомментированные примеры live для Ankara, Izmir, Gaziantep.

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
│   ├── adapters/            # base.py, registry.py, vfs_global.py, vfs_classifier.py
│   ├── mock_vfs/            # app.py, templates/ — учебный сайт
│   ├── db/                  # models.py, session.py, repositories/
│   ├── services/            # checks.py, slot_events.py, analytics.py, adapters_sync.py
│   ├── worker/              # main.py, scheduler.py, executor.py, browser.py, heartbeat.py
│   └── api/                 # main.py, routes/, schemas.py, templates/dashboard.html
├── tests/
│   ├── fixtures/vfs/*.html  # captcha_real.html (сохранён с реального сайта), остальные синтетические
│   ├── unit/
│   └── integration/
└── docs/
```

## 13. Тестирование

pytest + pytest-asyncio; интеграционные тесты на реальном PostgreSQL (`TEST_DATABASE_URL`, в compose — `docker compose run --rm tests`). Внешняя сеть в тестах не используется.

- **classify**: каждый статус на фикстурах, включая реальную страницу Cloudflare; маркеры из `config` переопределяются.
- **CheckResult**: инварианты §6.1.
- **VfsGlobalAdapter на mock-vfs** (Playwright, mock-vfs поднимается в тесте через uvicorn на свободном порту): каждый сценарий §6.4 даёт ожидаемый статус и шаг остановки; `slow`/`server-error` → `TransientSourceError`.
- **Защита входа**: при хосте `vfsglobal.com` и заданных учётных данных пароль не вводится, результат `LOGIN_REQUIRED`.
- **Фейковый адаптер** (только в тестах): для проверки ретраев, падений и очереди без браузера.
- **Очередь**: два конкурентных захвата не получают одну задачу; просроченная аренда перехватывается; `MAX_CLAIMS` → `failed`; дубль активной задачи не создаётся; планировщик сдвигает `next_run_at`.
- **Сервис проверки**: ретраи на `TransientSourceError` (`attempts=3`); без ретраев на CAPTCHA; падение адаптера → `ERROR`.
- **slot_events**: APPEARED / CHANGED / DISAPPEARED; повтор без события; CAPTCHA/ERROR не влияют.
- **API**: все endpoints через `httpx.AsyncClient`, включая дедупликацию ручной проверки и `?wait=true`.
- **Аналитика**: на заранее заполненных данных — точные цифры, `insufficient_data`, разделение live/demo.
- **Ручная проверка live**: один прогон против VFS вне тестов, результат и скриншот — в README.

## 14. Документация и демонстрация

- `README.md`: запуск (`docker compose up`), сценарий демо, примеры ответов API, добавление центра и оператора, масштабирование.
- `LIMITATIONS.md`: наблюдения §2, что не делаем и почему, шаги интеграции: официальный API или партнёрство с VFS, оператор с ручной сессией, уведомления по `slot_events`, переход на брокер при росте.

Сценарий созвона: `docker compose up` → `/docs` → `POST /adapters/vfs-…/check?wait=true` (live, `CAPTCHA_REQUIRED` + скриншот) → `POST /adapters/mock-available/check?wait=true` (браузер проходит вход и выбор центра → `AVAILABLE`, скриншот) → `mock-slow` (ретраи в логах → `ERROR`) → `/checks`, `/slot-events` → `/dashboard` Live/Demo → остановка worker'а (`/health` = degraded) → добавление Ankara в YAML. Для наглядности worker можно запустить локально с `HEADFUL=true` — окно браузера видно.
