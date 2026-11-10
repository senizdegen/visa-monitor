# Alay Visa Monitor

Backend-модуль мониторинга визовых записей. Первый адаптер: Турция -> Франция, VFS Global, Istanbul, Tourism.
Система по расписанию проверяет источник браузером (Playwright), хранит историю всех проверок и изменений доступности в PostgreSQL и отдаёт их через REST API и dashboard.
Ядро (очередь, планировщик, хранение, API, аналитика) не знает про конкретные сайты; всё, что специфично для источника, лежит в адаптере.

Важно: слоты на реальном VFS видны только после входа в аккаунт, а вход закрыт CAPTCHA, поэтому live-адаптер честно сохраняет то, что получил (сейчас `ERROR access_denied_403`). Статусы `AVAILABLE`, `NO_SLOTS`, `WAITLIST` показываются на локальном учебном сайте `mock-vfs`. Подробности: [LIMITATIONS.md](LIMITATIONS.md).

## Быстрый старт

Нужны Docker и Docker Compose. Первая сборка скачивает Chromium, это несколько минут.

```bash
docker compose up -d --build
```

Если порт 8000 на хосте занят, задайте другой через `API_PORT` (в `.env` или переменной окружения):

```bash
API_PORT=8010 docker compose up -d --build
```

Ниже везде `localhost:8000`; при `API_PORT=8010` подставьте 8010.

| URL | Что там |
|---|---|
| http://localhost:8000/docs | Swagger |
| http://localhost:8000/dashboard | аналитика, переключатель Live / Demo |
| http://localhost:8000/health | `ok` / `degraded` / `down` |
| http://localhost:8081/available/login | `mock-vfs`, учебный сайт (корень `/` пустой; сценарии: `available`, `no-slots`, `waitlist`, `captcha`, `login-wall`, `server-error`, `slow`, `changed-layout`, `rotate`) |

Через ~10 секунд после старта worker сам создаёт первые проверки. Одна ручная проверка:

```bash
curl -X POST "localhost:8000/adapters/mock-available/check?wait=true"
```

Остановка: `docker compose down` (данные остаются в томах), `docker compose down -v` (с удалением данных).

Kill switch для live-проверок: `LIVE_CHECKS_ENABLED=false` в `.env`, затем `docker compose up -d`. Live-адаптеры перестают планироваться, ручной live-запуск возвращает `409`.

## Архитектура

```
docker compose
├── postgres   — единственное хранилище и координатор очереди
├── migrate    — разово: alembic upgrade head + синхронизация adapters.yaml -> БД
├── api        — FastAPI: REST, Swagger (/docs), dashboard (/dashboard)
├── worker     — asyncio-процесс: планировщик + исполнитель + heartbeat, Playwright Chromium
└── mock-vfs   — учебный сайт, имитирующий путь записи VFS
```

`api` и `worker` — один пакет `alay` (`src/alay/`), разные точки входа. Между собой общаются только через PostgreSQL. Любой сервис можно запустить в нескольких экземплярах.

Поток данных:

1. `adapters.yaml` -> `migrate` делает upsert в таблицу `adapters` (после правки YAML: `docker compose run --rm migrate`).
2. Планировщик в worker'е раз в 5 с находит адаптеры с `next_run_at <= now()`, создаёт `check_jobs` (`pending`) и сдвигает `next_run_at`. Ручная проверка (`POST /adapters/{id}/check`) создаёт такую же задачу. На адаптер не больше одной активной задачи (уникальный индекс в БД).
3. Исполнитель забирает задачи через `SELECT ... FOR UPDATE SKIP LOCKED` с арендой (`JOB_LEASE_S`). Упавший worker: аренда истекает, задачу берёт другой; после `MAX_CLAIMS` задача `failed` и пишется `ERROR worker_lost`.
4. Сервис проверки запускает адаптер, ретраит только временные сбои (таймаут, сеть, 5xx; 3 попытки), в одной транзакции пишет `checks`, при изменении доступности `slot_events`, и закрывает задачу.
5. API читает те же таблицы; аналитика считается SQL-запросами.

Очередь сделана на PostgreSQL, а не на Celery/RabbitMQ: БД уже нужна, 100 адаптеров раз в час это около 1.7 задачи в минуту, а один сервис вместо трёх проще запускать и отлаживать.

## Таблицы

| Таблица | Что хранит |
|---|---|
| `adapters` | что и как часто проверять: метаданные, `source` (`live`/`demo`), `config`, `interval_minutes`, `enabled`, `next_run_at` |
| `check_jobs` | очередь: `pending`/`running`/`done`/`failed`, аренда (`locked_by`, `locked_until`), `claim_count` |
| `checks` | история всех проверок: статус, `available_dates`, `error`, `response_time_ms`, `duration_ms`, `attempts`, `evidence_path`, `raw` (диагностика) |
| `slot_events` | изменения доступности: `APPEARED` / `CHANGED` / `DISAPPEARED`, `dates_before`, `dates_after`. Пишутся только для определённых результатов (`AVAILABLE`, `NO_SLOTS`, `WAITLIST`); `CAPTCHA_REQUIRED`, `LOGIN_REQUIRED`, `ERROR`, `UNKNOWN` события не создают |
| `workers` | heartbeat worker'ов (для `/health`) |

Статусы: `AVAILABLE, NO_SLOTS, WAITLIST, LOGIN_REQUIRED, CAPTCHA_REQUIRED, ERROR, UNKNOWN`. Времена в БД — `timestamptz` в UTC, API отдаёт их в часовом поясе адаптера (`+03:00`). Даты в `available_dates` заполняются только при `AVAILABLE`, их нельзя выдумать (проверяет `CheckResult`).

## API

| Метод и путь | Описание |
|---|---|
| `GET /adapters` | адаптеры, расписание, последний статус |
| `POST /adapters/{id}/check[?wait=true]` | ручная проверка; `202 {job_id, ...}` или с `wait=true` `200` с результатом (ждёт до 60 с); `404` нет адаптера; `409` live при `LIVE_CHECKS_ENABLED=false` |
| `GET /jobs/{id}` | статус задачи и результат |
| `GET /checks` | история; фильтры `adapter_id, status, source, since, until, limit, offset` |
| `GET /availability` | последний результат по каждому включённому адаптеру |
| `GET /slot-events` | история изменений доступности |
| `GET /analytics?source=live\|demo&timeline=24h\|7d\|30d` | агрегаты за 24ч / 7д / 30д и временной ряд |
| `GET /health`, `GET /dashboard`, `GET /docs` | |
| `GET /evidence/<path>` | скриншоты проверок |

Примеры реальных ответов (захвачены с работающего стека, значения не редактировались):

- [`docs/examples/adapters.json`](docs/examples/adapters.json) — `GET /adapters`
- [`docs/examples/check-live.json`](docs/examples/check-live.json) — `POST /adapters/vfs-tur-fra-istanbul-tourism/check?wait=true`
- [`docs/examples/check-mock-available.json`](docs/examples/check-mock-available.json) — `POST /adapters/mock-available/check?wait=true`
- [`docs/examples/check-mock-slow.json`](docs/examples/check-mock-slow.json) — `mock-slow`: 3 попытки, затем `ERROR timeout`
- [`docs/examples/checks.json`](docs/examples/checks.json) — `GET /checks?limit=5`
- [`docs/examples/availability.json`](docs/examples/availability.json) — `GET /availability`
- [`docs/examples/slot-events.json`](docs/examples/slot-events.json) — `GET /slot-events`
- [`docs/examples/analytics-live.json`](docs/examples/analytics-live.json), [`analytics-demo.json`](docs/examples/analytics-demo.json) — `GET /analytics`
- [`docs/examples/health.json`](docs/examples/health.json) — `GET /health`
- [`docs/examples/live-vfs-403.png`](docs/examples/live-vfs-403.png) — скриншот реальной live-проверки

## Добавить центр

Новый центр — это ещё один блок в `adapters.yaml` с тем же `adapter_type` и другим `center`. Код менять не нужно.

```yaml
  - id: vfs-tur-fra-ankara-tourism
    adapter_type: vfs_global
    source: live
    source_country: Turkey
    destination_country: France
    provider: VFS Global
    center: Ankara
    visa_category: Tourism
    timezone: Europe/Istanbul
    interval_minutes: 60
    config:
      start_url: "https://visa.vfsglobal.com/tur/en/fra/login"
      category: "Short Stay"
      subcategory: "Tourism"
```

Применить (адаптер появится в `GET /adapters`, расписание подхватится без перезапуска worker'а):

```bash
docker compose run --rm migrate
```

Адаптеры, удалённые из YAML, получают `enabled=false`, история сохраняется.

## Добавить оператора

Новый оператор — модуль `src/alay/adapters/<name>.py` с классом, зарегистрированным по имени. Ядро, схема БД, API и аналитика не меняются.

```python
from alay.adapters.base import BaseAdapter
from alay.adapters.registry import register
from alay.domain import CheckResult, CheckStatus  # TransientSourceError тоже здесь


@register("my_operator")
class MyOperatorAdapter(BaseAdapter):
    async def check(self) -> CheckResult:
        # self.spec — строка adapters (метаданные + config), self.ctx — браузер, логи, evidence
        # Временный сбой (таймаут, сеть, 5xx): raise TransientSourceError — сервис повторит.
        # Всё остальное возвращается как CheckResult.
        return CheckResult(
            source_country=self.spec.source_country,
            destination_country=self.spec.destination_country,
            provider=self.spec.provider,
            center=self.spec.center,
            visa_category=self.spec.visa_category,
            status=CheckStatus.NO_SLOTS,
            source=self.spec.source,
        )
```

Затем в `adapters.yaml` указать `adapter_type: my_operator` и добавить импорт модуля в `src/alay/adapters/__init__.py` (так подключён `vfs_global`), иначе декоратор не выполнится. Точные имена и поля смотрите в `src/alay/adapters/base.py`, `registry.py`, `domain.py` (скелет показывает форму, а не готовый код).

## Масштабирование

- Больше адаптеров: блоки в `adapters.yaml`, ничего больше.
- Больше пропускной способности: `docker compose up -d --scale worker=3`. Worker'ы делят очередь через `SKIP LOCKED`, одну задачу два worker'а не возьмут. Внутри worker'а параллельность задаёт `WORKER_CONCURRENCY` (по умолчанию 3 вкладки Chromium).
- API stateless, его тоже можно запускать в нескольких экземплярах (нужен балансировщик/разные порты).
- Когда переходить на брокер (RabbitMQ/Redis + Celery или аналог): тысячи задач в минуту, приоритетные очереди, или нагрузка на Postgres от поллинга очереди становится заметной. Интерфейс адаптера при этом не меняется, меняется только способ доставки задач.

Конфигурация — переменные окружения, полный список с умолчаниями в `.env.example`.

## Тесты

152 теста, внешняя сеть не используется. Интеграционные тесты идут на реальном PostgreSQL.

В Docker (ничего устанавливать не надо):

```bash
docker compose --profile tests run --rm tests
```

Локально (нужны [uv](https://docs.astral.sh/uv/) и PostgreSQL на порту 5433 из compose):

```bash
docker compose up -d postgres
uv sync
uv run playwright install chromium
uv run pytest
```

## Сценарий демо

1. `docker compose up -d --build`, открыть `/docs`.
2. Live-проверка реального VFS:
   `curl -X POST "localhost:8000/adapters/vfs-tur-fra-istanbul-tourism/check?wait=true"`.
   Ответ содержит статус, `raw.rule`, `raw.http_status` и `evidence_url` со скриншотом (`/evidence/...`). На момент написания: `ERROR access_denied_403`, скриншот в `docs/examples/live-vfs-403.png`. Раньше с curl приходила страница Cloudflare Waiting Room с Turnstile, это `CAPTCHA_REQUIRED`.
3. Успешный путь на mock-vfs: `curl -X POST "localhost:8000/adapters/mock-available/check?wait=true"`. Браузер проходит вход и выбор центра, статус `AVAILABLE` с датами, скриншот в `evidence_url`.
4. Ошибки и ретраи: `curl -X POST "localhost:8000/adapters/mock-slow/check?wait=true"`. Занимает около 40 с; `docker compose logs worker` показывает три попытки, итог `ERROR timeout`, `attempts: 3`. Остальные сценарии: `mock-no-slots`, `mock-waitlist`, `mock-captcha`, `mock-login-wall`, `mock-server-error`, `mock-changed-layout`.
5. История: `/checks`, `/slot-events` (по расписанию работают только два адаптера: live `vfs-tur-fra-istanbul-tourism` раз в 60 минут и `mock-istanbul-tourism` раз в минуту, он проходит по сценариям по кругу, поэтому события появляются сами; остальные `mock-*` запускаются только вручную).
6. `/dashboard`: переключатель Live / Demo (в Demo постоянный баннер «Демонстрационные данные»), блок «Ограничения источника».
7. Отказоустойчивость: `docker compose stop worker`, `/health` показывает `degraded` (`workers_alive: 0`); `docker compose start worker` возвращает `ok`. При жёстком убийстве worker'а статус сменится, когда heartbeat станет старше 60 с.
8. Новый центр: раскомментировать Ankara в `adapters.yaml`, `docker compose run --rm migrate`, `GET /adapters`.

Для наглядности worker можно запустить локально с `HEADFUL=true`, тогда окно браузера видно (только вне Docker).

## Структура репозитория

```
adapters.yaml, docker-compose.yml, Dockerfile, .env.example
migrations/            Alembic
src/alay/
  adapters/            base, registry, vfs_global, vfs_classifier
  mock_vfs/            учебный сайт
  db/ services/        модели, репозитории, проверка, slot_events, аналитика
  worker/              планировщик, исполнитель, heartbeat, браузер
  api/                 FastAPI, dashboard
tests/                 unit, integration, fixtures/vfs (captcha_real.html — с реального сайта)
docs/examples/         реальные ответы API и скриншот
```
