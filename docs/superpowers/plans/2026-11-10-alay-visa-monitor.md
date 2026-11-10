# Alay Visa Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Async backend that periodically checks visa-appointment availability (Turkey → France, VFS Global, Istanbul) through pluggable adapters, stores every check, records slot changes, and serves REST API, analytics and a dashboard.

**Architecture:** One Python package `alay`, two processes (`api`, `worker`) coordinated only through PostgreSQL. PostgreSQL is also the job queue (`FOR UPDATE SKIP LOCKED` + leases). One Playwright adapter `VfsGlobalAdapter` drives either the real VFS site (stops at CAPTCHA/login) or the local `mock-vfs` training site (full flow, `source=demo`).

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2.0 async + asyncpg, Alembic, Playwright (async), tenacity, PyYAML, Jinja2, Chart.js, pytest + pytest-asyncio + httpx, Docker Compose, PostgreSQL 16.

**Spec:** `docs/superpowers/specs/2026-11-10-alay-visa-monitor-design.md` — read it before every task; section numbers below (§N) refer to it.

## Global Constraints

- Python `>=3.12`; package layout `src/alay/`; run everything via `uv run …`.
- Dependencies only from the Tech Stack line plus `uvicorn[standard]`, `tzdata` (Windows has no system tz DB). No Celery/Redis/RabbitMQ, no stealth/anti-bot plugins.
- All DB timestamps `timestamptz` in UTC; API renders them in the adapter's `timezone` (`Europe/Istanbul` → `+03:00`).
- Status values exactly: `AVAILABLE, NO_SLOTS, WAITLIST, LOGIN_REQUIRED, CAPTCHA_REQUIRED, ERROR, UNKNOWN`. Source values exactly: `live, demo`.
- Never fabricate dates: `available_dates` non-empty only with `AVAILABLE` (enforced in `CheckResult`).
- Automated login only when the start URL host is in `LOGIN_ALLOWED_HOSTS` (default `mock-vfs,localhost`).
- Env defaults (§10): `DEFAULT_INTERVAL_MINUTES=60`, `SCHEDULER_TICK_S=5`, `WORKER_CONCURRENCY=3`, `CHECK_TIMEOUT_S=90`, `CHECK_TOTAL_TIMEOUT_S=150`, `RETRY_ATTEMPTS=3`, `RETRY_WAIT_BASE_S=2`, `JOB_LEASE_S=180`, `MAX_CLAIMS=3`, `EXECUTOR_IDLE_S=1`, `HEARTBEAT_S=15`, `SHUTDOWN_GRACE_S=60`, `MANUAL_WAIT_S=60`, `MIN_CHECKS_FOR_STATS=5`, `LIVE_CHECKS_ENABLED=true`, `HEADFUL=false`, `EVIDENCE_DIR=/data/evidence` (local default `./data/evidence`).
- Tests never touch the internet. Integration tests use real PostgreSQL at `TEST_DATABASE_URL` (default `postgresql+asyncpg://alay:alay@localhost:5433/alay_test`).
- Logs: stdlib `logging`, one JSON object per line, include `job_id`/`adapter_id` when known.
- Commit after every task; message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Windows dev box without tz database** — `ZoneInfo("Europe/Istanbul")` must work; `tzdata` dependency + test in Task 10.
2. **Naive `since`/`until` query params** (`?since=2026-11-10T10:00`) — must be treated as UTC, not crash on naive/aware comparison; test in Task 10.
3. **Worker was down for hours** — on restart exactly one scheduled job per adapter, no catch-up flood; test in Task 8.
4. **`adapters.yaml` edits** — removed adapter becomes `enabled=false` with history kept; `interval_minutes: 0` or unknown keys fail sync with a message naming the adapter id; tests in Task 3.
5. **`POST …/check?wait=true` while no worker runs** — returns `202` after `MANUAL_WAIT_S`, never hangs or 500s; test in Task 10 (with `MANUAL_WAIT_S=1`).

---

## File Structure

```
pyproject.toml, uv.lock, .env.example, alembic.ini, adapters.yaml
docker-compose.yml, Dockerfile, docker/postgres-init/01-test-db.sql
migrations/env.py, migrations/versions/0001_initial.py
src/alay/
  config.py              Settings (pydantic-settings)
  logging.py             JSON log formatter + setup_logging()
  domain.py              CheckStatus, Source, CheckResult, AdapterSpec, TransientSourceError
  adapters/__init__.py   imports vfs_global so @register runs
  adapters/base.py       BaseAdapter, AdapterContext
  adapters/registry.py   register(), get_adapter_class(), UnknownAdapterType
  adapters/vfs_classifier.py  PageSnapshot, VfsMarkers, classify()
  adapters/vfs_global.py      VfsConfig, VfsSelectors, VfsGlobalAdapter
  mock_vfs/app.py, mock_vfs/scenarios.py, mock_vfs/templates/*.html
  db/models.py, db/session.py
  db/repo_adapters.py, db/repo_jobs.py, db/repo_checks.py
  services/adapters_sync.py  load_adapters_file(), sync_adapters()
  services/checks.py         CheckOutcome, run_check()
  services/slot_events.py    SlotEventType, compute_slot_event()
  services/analytics.py      compute_analytics()
  sync_adapters.py           `python -m alay.sync_adapters`
  worker/browser.py, worker/scheduler.py, worker/executor.py, worker/heartbeat.py, worker/main.py, worker/__main__.py
  api/main.py, api/deps.py, api/schemas.py, api/routes/{adapters,jobs,checks,analytics,health,dashboard}.py
  api/templates/dashboard.html
tests/conftest.py, tests/fakes.py
tests/fixtures/vfs/*.html
tests/unit/…, tests/integration/…
README.md, LIMITATIONS.md
```

---

### Task 1: Project scaffold, config, logging, test database

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.gitignore`, `docker-compose.yml` (only `postgres` service for now), `docker/postgres-init/01-test-db.sql`, `src/alay/__init__.py`, `src/alay/config.py`, `src/alay/logging.py`, `tests/conftest.py` (empty placeholder), `tests/unit/test_config.py`, `tests/unit/test_logging.py`

**Interfaces:**
- Produces: `alay.config.Settings` (fields = every env var in Global Constraints + `database_url`, `test_database_url`, `adapters_file`, `login_allowed_hosts: list[str]`, `mock_vfs_email="demo@alay.test"`, `mock_vfs_password="demo-password"`, `log_level="INFO"`); `alay.config.get_settings() -> Settings` (`functools.lru_cache`); `alay.logging.setup_logging(level: str) -> None`; `alay.logging.JsonFormatter`.

- [ ] **Step 1: Init uv project**

Run: `uv init --package --name alay --python 3.12 .` then `uv add fastapi "uvicorn[standard]" pydantic pydantic-settings "sqlalchemy[asyncio]" asyncpg alembic playwright tenacity pyyaml jinja2 httpx tzdata` and `uv add --dev pytest pytest-asyncio`.
In `pyproject.toml` add:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
testpaths = ["tests"]
```
`.gitignore`: `.venv/ __pycache__/ data/ .env .pytest_cache/`. `.gitattributes`: `* text=auto eol=lf` (the dev box is Windows; CRLF in shell/SQL files breaks Linux containers).

- [ ] **Step 2: Compose with Postgres**

`docker-compose.yml`: service `postgres` (`postgres:16`, env `POSTGRES_USER=alay POSTGRES_PASSWORD=alay POSTGRES_DB=alay`, port `5433:5432`, volume `pgdata`, healthcheck `pg_isready -U alay`, mounts `./docker/postgres-init:/docker-entrypoint-initdb.d:ro`). `01-test-db.sql`: `CREATE DATABASE alay_test;`.
Run: `docker compose up -d postgres` → Expected: `docker compose ps` shows postgres `healthy`.

- [ ] **Step 3: Write failing tests**

```python
# tests/unit/test_config.py
def test_defaults(monkeypatch):
    s = Settings(_env_file=None)
    assert s.default_interval_minutes == 60
    assert s.retry_attempts == 3 and s.retry_wait_base_s == 2
    assert s.job_lease_s == 180 and s.max_claims == 3
    assert s.login_allowed_hosts == ["mock-vfs", "localhost"]
    assert s.live_checks_enabled is True

def test_login_hosts_from_comma_env(monkeypatch):
    monkeypatch.setenv("LOGIN_ALLOWED_HOSTS", "a, b")
    assert Settings(_env_file=None).login_allowed_hosts == ["a", "b"]

# tests/unit/test_logging.py
def test_json_formatter_includes_extra_fields():
    rec = logging.LogRecord("alay", logging.INFO, __file__, 1, "check_finished", None, None)
    rec.adapter_id = "x"; rec.job_id = "j"
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "check_finished" and out["adapter_id"] == "x" and out["job_id"] == "j"
    assert out["level"] == "INFO" and "ts" in out
```

- [ ] **Step 4: Run tests — expect FAIL (import errors)**

Run: `uv run pytest tests/unit -v`

- [ ] **Step 5: Implement `Settings`, `get_settings`, `JsonFormatter`, `setup_logging`**

`login_allowed_hosts` parses a comma string (use `NoDecode` annotation + `field_validator(mode="before")`). `JsonFormatter` emits `ts` (UTC ISO), `level`, `logger`, `msg`, any record attribute not in the standard LogRecord set, and `exc` when `exc_info`. `.env.example` lists every variable with its default.

- [ ] **Step 6: Run tests — expect PASS**, then commit `chore: project scaffold, settings, json logging`.

---

### Task 2: Domain model, adapter base and registry

**Files:**
- Create: `src/alay/domain.py`, `src/alay/adapters/__init__.py` (empty for now), `src/alay/adapters/base.py`, `src/alay/adapters/registry.py`
- Test: `tests/unit/test_domain.py`, `tests/unit/test_registry.py`

**Interfaces:**
- Produces:
  - `class CheckStatus(StrEnum)` — 7 values; `class Source(StrEnum)` — `live`, `demo`.
  - `class AdapterSpec(BaseModel, extra="forbid")`: `id: str`, `adapter_type: str`, `source: Source`, `source_country: str`, `destination_country: str`, `provider: str`, `center: str`, `visa_category: str`, `timezone: str = "Europe/Istanbul"`, `config: dict = {}`, `interval_minutes: int = Field(gt=0)` (default from settings applied by sync), `enabled: bool = True`; method `result(self, status: CheckStatus, **fields) -> CheckResult` filling metadata + `source`.
  - `class CheckResult(BaseModel)`: fields from §6.1; `checked_at: AwareDatetime` default `datetime.now(UTC)`; `available_dates: list[date] = []`; `slots_count: int | None = None`; `has_waitlist: bool = False`; `error: str | None`; `response_time_ms: int | None`; `evidence_path: str | None`; `raw: dict = {}`.
  - `class TransientSourceError(Exception)` with `__init__(self, code: str, detail: str = "")`, attribute `code`.
  - `@dataclass class AdapterContext`: `settings: Settings`, `evidence_dir: Path`, `browser: "BrowserProvider | None" = None` (Task 9 defines BrowserProvider; type it as `Any` protocol with `async get() -> playwright Browser`).
  - `class BaseAdapter(ABC)`: `__init__(self, spec: AdapterSpec, ctx: AdapterContext)`; `abstract async def check(self) -> CheckResult`.
  - `register(name: str) -> Callable[[type[BaseAdapter]], type[BaseAdapter]]`; `get_adapter_class(name: str) -> type[BaseAdapter]` raises `UnknownAdapterType(name)`; `ADAPTER_TYPES: dict[str, type[BaseAdapter]]`. Registering the same name twice raises `ValueError`.

- [ ] **Step 1: Write failing tests**

```python
def test_dates_only_with_available(spec):
    with pytest.raises(ValidationError):
        spec.result(CheckStatus.CAPTCHA_REQUIRED, available_dates=[date(2026, 11, 18)])

def test_available_needs_dates_or_count(spec):
    with pytest.raises(ValidationError):
        spec.result(CheckStatus.AVAILABLE)
    assert spec.result(CheckStatus.AVAILABLE, slots_count=3).slots_count == 3

def test_available_dates_sorted_deduped_and_counted(spec):
    r = spec.result(CheckStatus.AVAILABLE, available_dates=[date(2026,11,20), date(2026,11,18), date(2026,11,20)])
    assert r.available_dates == [date(2026,11,18), date(2026,11,20)] and r.slots_count == 2

def test_error_requires_message(spec):
    with pytest.raises(ValidationError):
        spec.result(CheckStatus.ERROR)

def test_waitlist_sets_flag(spec):
    with pytest.raises(ValidationError):
        spec.result(CheckStatus.WAITLIST, has_waitlist=False)
    assert spec.result(CheckStatus.WAITLIST).has_waitlist is True   # defaulted by validator

def test_result_copies_metadata(spec):
    r = spec.result(CheckStatus.NO_SLOTS)
    assert (r.center, r.provider, r.source) == ("Istanbul", "VFS Global", Source.LIVE)
    assert r.checked_at.tzinfo is not None

def test_spec_rejects_zero_interval_and_unknown_keys():
    with pytest.raises(ValidationError): AdapterSpec(**BASE, interval_minutes=0)
    with pytest.raises(ValidationError): AdapterSpec(**BASE, interval_minutes=60, colour="red")

# test_registry.py
def test_register_and_lookup(): ...           # register("t-x") on a subclass; get_adapter_class("t-x") is it
def test_unknown_type_raises(): ...           # get_adapter_class("nope") raises UnknownAdapterType
def test_duplicate_name_raises(): ...
```
`spec` fixture: `AdapterSpec(id="vfs-tur-fra-istanbul-tourism", adapter_type="vfs_global", source="live", source_country="Turkey", destination_country="France", provider="VFS Global", center="Istanbul", visa_category="Tourism", interval_minutes=60)` — put it in `tests/conftest.py` as `adapter_spec`.

- [ ] **Step 2: Run — expect FAIL.** `uv run pytest tests/unit/test_domain.py tests/unit/test_registry.py -v`
- [ ] **Step 3: Implement** the interfaces above (invariants in one `model_validator(mode="after")`; WAITLIST with omitted `has_waitlist` → set True, explicit False → error — use `model_fields_set`).
- [ ] **Step 4: Run — expect PASS**, commit `feat: domain model and adapter registry`.

---

### Task 3: Database schema, migration, adapters.yaml sync

**Files:**
- Create: `src/alay/db/__init__.py`, `src/alay/db/models.py`, `src/alay/db/session.py`, `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_initial.py`, `src/alay/db/repo_adapters.py`, `src/alay/services/__init__.py`, `src/alay/services/adapters_sync.py`, `src/alay/sync_adapters.py`, `adapters.yaml` (first version: only the live Istanbul adapter)
- Modify: `tests/conftest.py`
- Test: `tests/integration/test_schema.py`, `tests/integration/test_adapters_sync.py`

**Interfaces:**
- Consumes: `AdapterSpec`, `Settings`.
- Produces:
  - ORM classes `AdapterRow`, `CheckJobRow`, `CheckRow`, `SlotEventRow`, `WorkerRow` with exactly the columns/indexes of §5.1–5.5 (`adapters` also has `source`); `Base`.
  - `create_engine(url: str) -> AsyncEngine`, `make_sessionmaker(engine) -> async_sessionmaker[AsyncSession]` (`expire_on_commit=False`).
  - `repo_adapters.upsert_specs(session, specs: list[AdapterSpec]) -> None`, `repo_adapters.list_adapters(session) -> list[AdapterRow]`, `repo_adapters.get_adapter(session, adapter_id) -> AdapterRow | None`, `repo_adapters.to_spec(row) -> AdapterSpec`.
  - `adapters_sync.load_adapters_file(path: Path, default_interval: int) -> list[AdapterSpec]` — raises `AdaptersFileError` whose message contains the adapter `id` (or list index) and the field.
  - `adapters_sync.sync_adapters(session, specs) -> None` — upsert; ids missing from file → `enabled=false`.
  - Test fixtures in `tests/conftest.py`: `engine` (session scope), `sessionmaker` (function scope, truncates all tables `RESTART IDENTITY CASCADE` before each test).

- [ ] **Step 1: Models + session + Alembic async env**

`migrations/env.py` uses the async template (`alembic init -t async`), reads URL from `ALEMBIC_DATABASE_URL` env or `Settings().database_url`, `target_metadata = Base.metadata`. Write `0001_initial.py` (autogenerate with postgres running, then hand-check): partial unique index `uq_check_jobs_active` on `check_jobs(adapter_id) WHERE status IN ('pending','running')`; check constraints for `status`/`source`/`trigger`/`event_type` value sets; `adapters.interval_minutes > 0`.

- [ ] **Step 2: Test fixtures**

`engine` fixture: connect to `TEST_DATABASE_URL`, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`, then `await asyncio.to_thread(alembic.command.upgrade, cfg, "head")` with `ALEMBIC_DATABASE_URL` set to the test URL (alembic's env calls `asyncio.run`, so it must run in a thread).

- [ ] **Step 3: Write failing tests**

```python
async def test_tables_exist(sessionmaker): # all 5 tables queryable
async def test_one_active_job_per_adapter(sessionmaker):
    # insert adapter + pending job; second pending job → IntegrityError; after first set done → insert succeeds
async def test_sync_inserts_and_sets_next_run_now(sessionmaker, adapter_spec)
async def test_sync_preserves_next_run_at_on_resync(sessionmaker, adapter_spec)
    # set next_run_at to +1h, sync again with changed interval → interval updated, next_run_at unchanged
async def test_sync_disables_removed_adapter_and_keeps_history(sessionmaker, adapter_spec)
def test_load_file_applies_default_interval(tmp_path)
def test_load_file_error_names_adapter(tmp_path):
    # yaml with id "bad-one" and interval_minutes: 0 → AdaptersFileError, "bad-one" in str(err)
```
YAML format: top-level `adapters:` list of `AdapterSpec` dicts.

- [ ] **Step 4: Run — expect FAIL.** `uv run pytest tests/integration -v`
- [ ] **Step 5: Implement** repo + sync (`INSERT … ON CONFLICT (id) DO UPDATE` excluding `next_run_at`), and `python -m alay.sync_adapters` (reads settings, loads file, syncs, logs count). `adapters.yaml`: live Istanbul adapter per §10 with `config: {start_url: "https://visa.vfsglobal.com/tur/en/fra/login", category: "Short Stay", subcategory: "Tourism"}` and commented Ankara/Izmir/Gaziantep examples.
- [ ] **Step 6: Run — expect PASS.** Also `uv run alembic upgrade head` against dev DB succeeds. Commit `feat: database schema, migrations, adapters sync`.

---

### Task 4: VFS page classifier

**Files:**
- Create: `src/alay/adapters/vfs_classifier.py`, `tests/fixtures/vfs/captcha_real.html`, `tests/fixtures/vfs/{login,no_slots,waitlist,available,unknown}.html`, `tests/fixtures/vfs/README.md` (which file is real, which synthetic)
- Test: `tests/unit/test_vfs_classifier.py`

**Interfaces:**
- Consumes: `AdapterSpec.result`, `CheckStatus`, `TransientSourceError`.
- Produces:
  - `@dataclass(frozen=True) class PageSnapshot`: `final_url: str`, `http_status: int | None`, `html: str`, `step: str`, `result_text: str | None = None`, `response_time_ms: int | None = None`.
  - `class VfsMarkers(BaseModel)` with defaults: `captcha = ["challenge-platform", "cf-turnstile", "Waiting Room", "Just a moment"]`, `no_slots = ["No appointment slots are currently available"]`, `waitlist = ["waitlist"]`, `available = ["Earliest available slot"]`; matching is case-insensitive substring.
  - `classify(snapshot: PageSnapshot, spec: AdapterSpec, markers: VfsMarkers) -> CheckResult` — rules in §6.3 order; raises `TransientSourceError("http_5xx", …)` for 5xx. Rules 6–8 search `result_text` when present, else page text. Dates: regex `\b(\d{2})-(\d{2})-(\d{4})\b` (DD-MM-YYYY), invalid dates skipped. Rule 8 with marker but zero parseable dates → `UNKNOWN`. Login detection: `type=["']?password` in html, or `/login` in the final URL path. `raw` = `{"rule": <name>, "step": snapshot.step, "http_status": …, "final_url": …}`; `response_time_ms` copied.

- [ ] **Step 1: Save the real Cloudflare page** — `curl -s -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0" https://visa.vfsglobal.com/tur/en/fra/login -o tests/fixtures/vfs/captcha_real.html`; confirm it contains `challenge-platform` (one-time fetch, not part of tests). Write synthetic fixtures with the exact texts from §6.4 / the mock site (Task 5 reuses these texts).
- [ ] **Step 2: Write failing tests** — one per rule:

```python
def test_real_cloudflare_page_is_captcha(): status CAPTCHA_REQUIRED, raw["rule"] == "captcha"
def test_captcha_wins_over_403_and_503():   # captcha html with http_status=403 and 503 → CAPTCHA_REQUIRED, no raise
def test_5xx_without_challenge_raises_transient()
def test_429_is_error_rate_limited():        error == "rate_limited"
def test_403_plain_is_access_denied():       error == "access_denied_403"
def test_login_form_is_login_required()
def test_no_slots(); def test_waitlist()     # waitlist → has_waitlist True
def test_available_parses_dates():            "…is : 18-11-2026 … 20-11-2026" → [2026-11-18, 2026-11-20]
def test_available_marker_without_dates_is_unknown()
def test_result_text_preferred_over_page():   page html contains "No appointment slots…" in a footer, result_text has available text → AVAILABLE
def test_unknown_when_nothing_matches()
def test_custom_markers_override_defaults()
```
- [ ] **Step 3: Run — expect FAIL.** `uv run pytest tests/unit/test_vfs_classifier.py -v`
- [ ] **Step 4: Implement** `classify` (page text = html with tags stripped via regex `<[^>]+>` and `html.unescape`).
- [ ] **Step 5: Run — expect PASS**, commit `feat: VFS page classifier with fixtures`.

---

### Task 5: mock-vfs training site

**Files:**
- Create: `src/alay/mock_vfs/__init__.py`, `src/alay/mock_vfs/scenarios.py`, `src/alay/mock_vfs/app.py`, `src/alay/mock_vfs/templates/{base,login,waiting_room,dashboard,booking}.html`
- Test: `tests/integration/test_mock_vfs.py` (httpx `ASGITransport`, no browser)

**Interfaces:**
- Produces:
  - `SCENARIOS: tuple[str, ...] = ("available", "available-one", "no-slots", "waitlist", "captcha", "login-wall", "server-error", "slow", "changed-layout")`; `ROTATION = ("no-slots", "available", "available-one", "no-slots", "waitlist", "captcha", "server-error")`.
  - `resolve_scenario(name: str, now: datetime) -> str` — `rotate` → `ROTATION[now.minute % len(ROTATION)]`; unknown → `KeyError` (404).
  - `slot_message(scenario: str, centre: str, today: date) -> tuple[str, list[date]]` — `available`: dates today+8, today+10, text `"Earliest available slot for {centre} is : DD-MM-YYYY. Other available dates: DD-MM-YYYY"`; `available-one`: today+10 only; `no-slots`/`login-wall`: `"No appointment slots are currently available"`; `waitlist`: `"All slots are booked. Waitlist is open for this category."`.
  - `create_app(*, email: str, password: str, slow_seconds: float = 120, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> FastAPI`. Module-level `app = create_app(email=…, password=…)` from `Settings` for uvicorn.
- Routes (all HTML pages carry banner `Mock VFS — test site, not affiliated with VFS Global`):
  - `GET /{scenario}/login`: `captcha` → 403 + waiting-room page containing `challenge-platform`, `cf-turnstile`, "Waiting Room"; `server-error` → 503 plain; `slow` → `await asyncio.sleep(slow_seconds)` then login page; otherwise login page whose form (labels `Email`, `Password`, button `Sign In`, `<input type="password">`) is injected by inline JS after 500 ms.
  - `POST /{scenario}/login` (form `email`, `password`): match → cookie `mock_session=1`, `303` to `/{scenario}/dashboard`; else login page with error text.
  - `GET /{scenario}/dashboard`: no cookie → `303` to login. Button `Start New Booking` (link to `/{scenario}/booking`); `changed-layout` renders `Begin Appointment` instead.
  - `GET /{scenario}/booking`: three native `<select>` with `<label for>`: `Application Centre` (Istanbul, Ankara, Izmir, Gaziantep), `Appointment Category` (Short Stay, Long Stay), `Sub-category` (Tourism, Business, Family Visit). JS: when all three chosen, `fetch('/api/slots?scenario=…&centre=…')` and render `<div class="alert" data-testid="slot-result">{message}</div>`.
  - `GET /api/slots?scenario=&centre=` → `{"message": str, "dates": ["YYYY-MM-DD"]}`.
  - Scenario path segment `rotate` is resolved once per request with `clock()`.

- [ ] **Step 1: Write failing tests**

```python
async def test_captcha_login_returns_403_waiting_room(client)
async def test_server_error_returns_503(client)
async def test_login_then_dashboard_flow(client)       # POST creds → 303 → dashboard has "Start New Booking"
async def test_wrong_password_stays_on_login(client)
async def test_dashboard_requires_session(client)       # → 303 to login
async def test_changed_layout_renames_button(client)
async def test_api_slots_available_dates_relative_to_today(client)   # clock fixed 2026-11-10 → 2026-11-18, 2026-11-20
async def test_rotate_follows_minute(client)            # clock minute=1 → "available"
async def test_every_page_has_banner(client)
async def test_slow_scenario_delays(client)             # slow_seconds=0.2 → elapsed >= 0.2
```
- [ ] **Step 2: Run — expect FAIL.** `uv run pytest tests/integration/test_mock_vfs.py -v`
- [ ] **Step 3: Implement** (Jinja2Templates; minimal CSS; no VFS logos or brand colours).
- [ ] **Step 4: Run — expect PASS.** Manual: `uv run uvicorn alay.mock_vfs.app:app --port 8081`, open `http://localhost:8081/available/login`, walk the flow. Commit `feat: mock-vfs training site`.

---

### Task 6: VfsGlobalAdapter (Playwright)

**Files:**
- Create: `src/alay/adapters/vfs_global.py`; Modify: `src/alay/adapters/__init__.py` (import `vfs_global`)
- Create: `src/alay/worker/__init__.py`, `src/alay/worker/browser.py` (needed here for tests)
- Test: `tests/integration/test_vfs_adapter_mock.py`, `tests/unit/test_login_guard.py`

**Interfaces:**
- Consumes: `classify`, `PageSnapshot`, `VfsMarkers`, `BaseAdapter`, `AdapterContext`, `register`, mock-vfs `create_app`.
- Produces:
  - `class BrowserProvider`: `__init__(self, headful: bool = False)`; `async get(self) -> Browser` (lazy `async_playwright().start()` + `chromium.launch(headless=not headful)`, guarded by `asyncio.Lock`); `async close(self) -> None`.
  - `class VfsSelectors(BaseModel)`: `email_label="Email"`, `password_label="Password"`, `sign_in="Sign In"`, `start_booking="Start New Booking"`, `centre_label="Application Centre"`, `category_label="Appointment Category"`, `subcategory_label="Sub-category"`, `result="[data-testid=slot-result], .alert"`.
  - `class VfsConfig(BaseModel)`: `start_url: str`, `category: str`, `subcategory: str`, `login: bool = False`, `timeout_s: float = 30`, `selectors: VfsSelectors = VfsSelectors()`, `markers: VfsMarkers = VfsMarkers()`.
  - `login_allowed(start_url: str, allowed_hosts: list[str]) -> bool` (exact hostname match).
  - `@register("vfs_global") class VfsGlobalAdapter(BaseAdapter)` — `check()` implements the §6.3 step table: new `browser.new_context()`; step `open` (`page.goto`, measure `response_time_ms`, keep `http_status`); if classify of the first page is not `LOGIN_REQUIRED`/`UNKNOWN`, return it; step `login` — if not `config.login` or not `login_allowed(...)` → return `LOGIN_REQUIRED` with `raw["login_blocked"] = True` when credentials were requested but host not allowed; else fill `Email`/`Password` from `settings.mock_vfs_*`, click `Sign In`; steps `start_booking`, `select` (`get_by_label(...).select_option(label=...)` for centre = `spec.center`, category, subcategory), `result` (`locator(result).wait_for()` and read `inner_text()` into `result_text`). Any `playwright TimeoutError` after the `open` step stops the flow: snapshot current page with `step` = failed step name, classify. `page.goto` timeout or `net::` error → `TransientSourceError("timeout"|"network")`. Always: screenshot to `ctx.evidence_dir / spec.id / f"{checked_at:%Y%m%dT%H%M%S}.png"`, store path relative to `evidence_dir` in `evidence_path`; close context in `finally`.

- [ ] **Step 1: Install browser** — `uv run playwright install chromium`.
- [ ] **Step 2: Write failing tests**

Fixture `mock_site`: start `uvicorn.Server` for `create_app(email, password, slow_seconds=5)` on a free port in a background task; yields base URL. Fixture `browser` = session-scoped `BrowserProvider`. Helper builds a demo `AdapterSpec` with `config={"start_url": f"{base}/{scenario}/login", "category": "Short Stay", "subcategory": "Tourism", "login": True, "timeout_s": 2}` and settings with `login_allowed_hosts=["127.0.0.1"]`.

```python
@pytest.mark.parametrize("scenario,status,step", [
    ("available", "AVAILABLE", "result"), ("no-slots", "NO_SLOTS", "result"),
    ("waitlist", "WAITLIST", "result"), ("captcha", "CAPTCHA_REQUIRED", "open"),
    ("changed-layout", "UNKNOWN", "start_booking")])
async def test_scenarios(scenario, status, step, ...): r.status == status and r.raw["step"] == step

async def test_available_dates_are_real_from_page(...)   # == today+8, today+10
async def test_login_wall_without_login_config(...)      # login=False → LOGIN_REQUIRED, step "login"
async def test_server_error_raises_transient(...)
async def test_slow_raises_transient_timeout(...)        # timeout_s=1, slow_seconds=5
async def test_screenshot_written(...)                   # evidence file exists, path relative
# tests/unit/test_login_guard.py
def test_vfs_host_never_allowed(): login_allowed("https://visa.vfsglobal.com/tur/en/fra/login", ["mock-vfs","localhost"]) is False
def test_mock_host_allowed()
async def test_adapter_refuses_login_on_disallowed_host(...)  # mock site but allowed_hosts=["mock-vfs"], login=True → LOGIN_REQUIRED, raw["login_blocked"] is True, password field never filled
```
- [ ] **Step 3: Run — expect FAIL.** `uv run pytest tests/integration/test_vfs_adapter_mock.py tests/unit/test_login_guard.py -v`
- [ ] **Step 4: Implement** `BrowserProvider`, `VfsConfig`, `login_allowed`, `VfsGlobalAdapter`. Parse config with `VfsConfig.model_validate(spec.config)`; invalid config → `ValueError` (service turns it into ERROR).
- [ ] **Step 5: Run — expect PASS**, commit `feat: Playwright VFS adapter with mock-vfs tests`.

---

### Task 7: Check service and slot-event diff

**Files:**
- Create: `src/alay/services/checks.py`, `src/alay/services/slot_events.py`, `tests/fakes.py`
- Test: `tests/unit/test_check_service.py`, `tests/unit/test_slot_events.py`

**Interfaces:**
- Consumes: registry, `AdapterSpec`, `AdapterContext`, `TransientSourceError`, `Settings`.
- Produces:
  - `@dataclass class CheckOutcome`: `result: CheckResult`, `attempts: int`, `duration_ms: int`.
  - `async run_check(spec: AdapterSpec, ctx: AdapterContext) -> CheckOutcome` — §7.3 steps 1–3. tenacity `AsyncRetrying(retry=retry_if_exception_type(TransientSourceError | TimeoutError), stop=stop_after_attempt(s.retry_attempts), wait=wait_exponential(multiplier=s.retry_wait_base_s, min=s.retry_wait_base_s), reraise=True)`; each attempt in `asyncio.timeout(s.check_timeout_s)`; all in `asyncio.timeout(s.check_total_timeout_s)`. Errors → `ERROR` with `error` = `"<code>: <detail>"` for transient, `"timeout"`, `"total_timeout"`, `"unknown_adapter_type: <name>"`, `"adapter_crash: <ExcType>: <msg>"` (log with traceback). Never raises.
  - `class SlotEventType(StrEnum)`: `APPEARED, CHANGED, DISAPPEARED`.
  - `compute_slot_event(previous: list[date], result: CheckResult) -> tuple[SlotEventType, list[date], list[date]] | None` — rules §5.4.
  - `tests/fakes.py`: `@register("fake") class FakeAdapter` reading `spec.config["steps"]`: list of `"NO_SLOTS"`, `"AVAILABLE:2026-12-01,2026-12-03"`, `"WAITLIST"`, `"CAPTCHA_REQUIRED"`, `"transient"`, `"crash"`, `"sleep:<seconds>"`; per-`spec.id` call counter in a class-level dict; `FakeAdapter.reset()`.

- [ ] **Step 1: Write failing tests** (settings with `retry_wait_base_s=0`, `check_timeout_s=0.5`)

```python
async def test_retries_transient_then_succeeds():  steps ["transient","transient","NO_SLOTS"] → NO_SLOTS, attempts == 3
async def test_gives_up_after_three():             3× transient → ERROR, attempts == 3, error startswith "fake_transient"
async def test_captcha_not_retried():              ["CAPTCHA_REQUIRED"] → attempts == 1
async def test_crash_becomes_error():              ["crash"] → ERROR, error startswith "adapter_crash"
async def test_attempt_timeout_is_retried():       ["sleep:2","NO_SLOTS"] → NO_SLOTS, attempts == 2
async def test_unknown_type():                     adapter_type "nope" → ERROR "unknown_adapter_type: nope"
async def test_duration_measured():                duration_ms >= 0
# slot events
def test_appeared(); def test_changed(); def test_disappeared_on_no_slots(); def test_waitlist_counts_as_empty()
def test_same_dates_unordered_no_event()
@pytest.mark.parametrize("status", ["CAPTCHA_REQUIRED","LOGIN_REQUIRED","ERROR","UNKNOWN"])
def test_indeterminate_status_never_creates_event(status)
```
- [ ] **Step 2: Run — expect FAIL.** `uv run pytest tests/unit/test_check_service.py tests/unit/test_slot_events.py -v`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run — expect PASS**, commit `feat: check service with retries and slot-event diff`.

---

### Task 8: Job queue repository and result persistence

**Files:**
- Create: `src/alay/db/repo_jobs.py`, `src/alay/db/repo_checks.py`
- Test: `tests/integration/test_queue.py`, `tests/integration/test_save_outcome.py`

**Interfaces:**
- Consumes: ORM rows, `AdapterSpec`, `CheckOutcome`, `compute_slot_event`, `to_spec`.
- Produces:
  - `@dataclass class ClaimedJob`: `job_id: UUID`, `spec: AdapterSpec`, `trigger: str`, `claim_count: int`.
  - `repo_jobs.enqueue_due(session, *, live_enabled: bool) -> int` — §7.1 in one transaction; when `live_enabled` is False, `source='live'` adapters are not selected at all (no job, `next_run_at` untouched). Returns jobs created.
  - `repo_jobs.create_manual_job(session, adapter_id: str) -> tuple[CheckJobRow, bool]` — returns existing active job with `deduplicated=True` if present (race-safe: on `IntegrityError` re-select).
  - `repo_jobs.claim_jobs(session, *, worker_id: str, limit: int, lease_s: int) -> list[ClaimedJob]` — §7.2 SQL.
  - `repo_jobs.fail_exhausted_jobs(session, *, max_claims: int) -> int` — running jobs with expired lease and `claim_count >= max_claims` → `failed`, `error="worker_lost"`, plus a `checks` row `ERROR worker_lost` (attempts 0, duration 0).
  - `repo_jobs.get_job(session, job_id) -> CheckJobRow | None`.
  - `repo_checks.save_outcome(session, *, job_id: UUID, worker_id: str, spec: AdapterSpec, outcome: CheckOutcome) -> bool` — one transaction: `SELECT … FOR UPDATE` the job; if `status != 'running'` or `locked_by != worker_id` → rollback, return False; insert `checks`; previous dates = `dates_after` of latest `slot_events` for adapter or `[]`; insert event if `compute_slot_event` returns one; job `done`, `finished_at=now()`, clear lock.
  - `repo_checks.list_checks(session, *, adapter_id=None, status=None, source=None, since=None, until=None, limit=50, offset=0) -> list[CheckRow]`, `repo_checks.latest_per_adapter(session, *, source=None, enabled_only=True) -> list[CheckRow]` (`DISTINCT ON (adapter_id)`), `repo_checks.list_slot_events(session, *same filters minus status*) -> list[SlotEventRow]`, `repo_checks.check_for_job(session, job_id) -> CheckRow | None`.

- [ ] **Step 1: Write failing tests**

```python
async def test_enqueue_due_creates_job_and_advances_next_run()   # next_run_at ≈ now()+60min
async def test_enqueue_after_long_downtime_creates_one_job()     # next_run_at = now()-5h → exactly 1 job; second call → 0
async def test_enqueue_skips_disabled_and_live_when_off()
async def test_enqueue_does_not_duplicate_active_job()           # pending job exists, next_run due → 0 new, next_run still advanced
async def test_manual_job_dedup()                                # second call → same id, deduplicated True
async def test_concurrent_claims_never_share_a_job():
    # 10 pending jobs; asyncio.gather(claim(worker a, limit 10), claim(worker b, limit 10)) in two sessions → disjoint, union == 10
async def test_expired_lease_is_reclaimed()                      # lease_s=0 → second claim gets it, claim_count == 2
async def test_exhausted_job_fails_with_error_check()            # claim_count=3, expired → failed + checks ERROR worker_lost
async def test_save_outcome_writes_check_and_marks_done()
async def test_save_outcome_rejected_when_lease_stolen()         # locked_by changed → False, no check row
async def test_save_outcome_event_sequence():
    # NO_SLOTS, AVAILABLE[a,b], AVAILABLE[a,b], CAPTCHA, AVAILABLE[b], NO_SLOTS → events APPEARED, CHANGED, DISAPPEARED (3 rows)
async def test_list_checks_filters_and_order()
async def test_latest_per_adapter()
```
- [ ] **Step 2: Run — expect FAIL.** `uv run pytest tests/integration/test_queue.py tests/integration/test_save_outcome.py -v`
- [ ] **Step 3: Implement** (raw SQL via `sqlalchemy.text` is fine for the two `SKIP LOCKED` statements; everything else via ORM `select`/`insert`).
- [ ] **Step 4: Run — expect PASS**, commit `feat: postgres job queue and result persistence`.

---

### Task 9: Worker process

**Files:**
- Create: `src/alay/worker/scheduler.py`, `src/alay/worker/executor.py`, `src/alay/worker/heartbeat.py`, `src/alay/worker/main.py`, `src/alay/worker/__main__.py`
- Test: `tests/integration/test_worker.py`

**Interfaces:**
- Consumes: `enqueue_due`, `claim_jobs`, `fail_exhausted_jobs`, `save_outcome`, `run_check`, `BrowserProvider`, `AdapterContext`.
- Produces:
  - `async scheduler_loop(sm, settings, stop: asyncio.Event) -> None` — every `scheduler_tick_s`; DB errors logged, loop continues.
  - `class Executor`: `__init__(self, sm, settings, ctx: AdapterContext, worker_id: str)`; `async run(self, stop: asyncio.Event) -> None` — semaphore `worker_concurrency`; each loop: `fail_exhausted_jobs`, claim `free` jobs, spawn task per job (`run_check` → `save_outcome`, log `check_finished` with `job_id`, `adapter_id`, `status`, `attempts`, `duration_ms`); idle sleep `executor_idle_s` (interruptible by `stop`). On stop: stop claiming, await in-flight tasks up to `shutdown_grace_s`, then return.
  - `async heartbeat_loop(sm, worker_id, settings, stop) -> None` — upsert `workers` every `heartbeat_s`; delete own row on exit.
  - `async run_worker(settings: Settings, stop: asyncio.Event) -> None` — builds engine, `BrowserProvider(settings.headful)`, `worker_id = f"{socket.gethostname()}-{os.getpid()}"`, runs the three loops in `asyncio.TaskGroup`, closes browser and engine.
  - `__main__.py`: `setup_logging`, create `stop`, SIGTERM/SIGINT → `stop.set()` (`loop.add_signal_handler`; on Windows fall back to `signal.signal`), `asyncio.run(...)`.

- [ ] **Step 1: Write failing tests** (settings: ticks 0.1 s, `retry_wait_base_s=0`, `shutdown_grace_s=5`; adapters of type `fake` synced into the DB)

```python
async def test_scheduled_check_runs_end_to_end():   # adapter due now, steps ["NO_SLOTS"]; run worker ~1.5 s → one checks row NO_SLOTS, job done
async def test_manual_job_processed()
async def test_heartbeat_row_present_then_removed_on_stop()
async def test_stop_waits_for_inflight_check():     # steps ["sleep:1","NO_SLOTS"]... set stop right after claim → check still saved, run_worker returns < shutdown_grace_s
async def test_one_failing_adapter_does_not_block_others()  # adapter A ["crash"], B ["NO_SLOTS"] → both have checks rows
```
- [ ] **Step 2: Run — expect FAIL.** `uv run pytest tests/integration/test_worker.py -v`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run — expect PASS.** Manual: `uv run python -m alay.sync_adapters && uv run python -m alay.worker` against dev DB with live adapter → log line with `CAPTCHA_REQUIRED` within a minute; Ctrl+C exits cleanly. Commit `feat: worker with scheduler, executor and heartbeat`.

---

### Task 10: REST API

**Files:**
- Create: `src/alay/api/__init__.py`, `src/alay/api/main.py`, `src/alay/api/deps.py`, `src/alay/api/schemas.py`, `src/alay/api/routes/{__init__,adapters,jobs,checks,health}.py`
- Test: `tests/integration/test_api.py`

**Interfaces:**
- Consumes: repos from Tasks 3 and 8.
- Produces:
  - `create_app(settings: Settings | None = None, sessionmaker=None) -> FastAPI` (lifespan creates engine when sessionmaker not given; mounts `StaticFiles` at `/evidence` from `evidence_dir`, `check_dir=False`).
  - `schemas.to_local(dt: datetime, tz: str) -> datetime`; `CheckOut` = §6.1 fields + `id`, `adapter_id`, `job_id`, `attempts`, `duration_ms`, `evidence_url` (`/evidence/<path>` or null), times in adapter tz; `AdapterOut`, `JobOut` (`id, adapter_id, trigger, status, claim_count, created_at, started_at, finished_at, error, result: CheckOut | None`), `ManualCheckOut` (`job_id, status, deduplicated, result: CheckOut | None`), `SlotEventOut`, `HealthOut` (`status: Literal["ok","degraded","down"]`, `database: bool`, `workers_alive: int`, `stale_pending_jobs: int`).
  - Endpoints per §8 (minus `/analytics`, `/dashboard` — Tasks 11–12). `since`/`until` naive → UTC. `limit` 1..500.

- [ ] **Step 1: Write failing tests** (httpx `AsyncClient(transport=ASGITransport(create_app(settings, sessionmaker)))`)

```python
async def test_list_adapters_with_last_status()
async def test_manual_check_202_and_dedup()          # second POST → same job_id, deduplicated true
async def test_manual_check_404_unknown_adapter()
async def test_manual_check_409_live_when_disabled() # LIVE_CHECKS_ENABLED=false + live adapter
async def test_manual_check_allowed_for_disabled_adapter()  # enabled=false demo adapter → 202
async def test_wait_returns_202_when_no_worker()     # MANUAL_WAIT_S=1, nothing processes → 202 within ~1.5 s
async def test_wait_returns_200_with_result()        # background task processes job via run_check+save_outcome → 200 with result
async def test_get_job_with_result(); async def test_get_job_404()
async def test_checks_filters_pagination_and_limit_cap()   # limit=501 → 422
async def test_naive_since_treated_as_utc()
async def test_times_rendered_in_adapter_timezone()  # checked_at ends with "+03:00"
async def test_availability_latest_per_adapter()
async def test_slot_events_endpoint()
async def test_health_ok_degraded_down()             # fresh heartbeat → ok; none → degraded; broken sessionmaker → down (HTTP 503)
async def test_swagger_available()                   # GET /openapi.json lists all paths
```
- [ ] **Step 2: Run — expect FAIL.** `uv run pytest tests/integration/test_api.py -v`
- [ ] **Step 3: Implement.** `?wait=true` polls `check_for_job` every 0.5 s up to `manual_wait_s` using a fresh session per poll.
- [ ] **Step 4: Run — expect PASS**, commit `feat: REST API`.

---

### Task 11: Analytics

**Files:**
- Create: `src/alay/services/analytics.py`, `src/alay/api/routes/analytics.py`; Modify: `src/alay/api/main.py`, `src/alay/api/schemas.py`
- Test: `tests/integration/test_analytics.py`

**Interfaces:**
- Produces:
  - `async compute_analytics(session, *, source: Source, adapter_id: str | None, now: datetime, timeline: Literal["24h","7d","30d"] = "24h", min_checks: int) -> AnalyticsOut`.
  - `AnalyticsOut` matches the §9 JSON: `source`, `generated_at`, `periods: dict["24h"|"7d"|"30d", PeriodStats]`, `timeline: list[TimelineBucket]` (`bucket`, `checks`, `available`, `errors`, `by_status`), `limitations: list[str]`.
  - `PeriodStats`: `checks_total`, `by_status: dict[str,int]`, `technical_success_rate: float | None`, `errors`, `avg_response_time_ms: float | None`, `p95_response_time_ms: float | None` (`percentile_cont(0.95)`), `avg_duration_ms: float | None`, `available_observations`, `slot_events`, `insufficient_data: bool`.
  - Timeline buckets: hour for `24h`, day for `7d`/`30d`, via `date_trunc(..., checked_at AT TIME ZONE 'Europe/Istanbul')`; buckets with zero checks included (generate_series) so the chart has no gaps.
  - Limitations rules (exact strings, `{n}`/`{total}` filled): no checks → `"Нет проверок за период {period}."`; CAPTCHA+LOGIN share > 50% in 24h → `"За 24ч {n} из {total} проверок остановлены CAPTCHA или логином: данные о слотах недоступны."`; `source=demo` → `"Демонстрационные данные mock-vfs, не реальные наблюдения."`.
  - `GET /analytics?source=live&adapter_id=&timeline=24h`.

- [ ] **Step 1: Write failing tests** (insert `checks` rows directly with fixed `checked_at`; pass fixed `now`)

```python
async def test_period_counts_and_success_rate():   # 10 live: 8 CAPTCHA, 1 ERROR, 1 UNKNOWN → total 10, success 0.8, errors 1
async def test_insufficient_data_nulls_rates():    # 3 checks → insufficient_data True, technical_success_rate None
async def test_live_and_demo_never_mixed():        # demo AVAILABLE rows don't change live available_observations
async def test_p95_and_avg_response_time()
async def test_periods_window_boundaries():        # check 2 days old counts in 7d and 30d, not 24h
async def test_slot_event_counts()
async def test_timeline_hourly_has_24_buckets_with_zero_fill()
async def test_captcha_limitation_message()
async def test_demo_limitation_message()
async def test_analytics_endpoint_defaults_to_live()
```
- [ ] **Step 2: Run — expect FAIL.** `uv run pytest tests/integration/test_analytics.py -v`
- [ ] **Step 3: Implement** (aggregates with `COUNT(*) FILTER (WHERE …)`; one query per period is fine).
- [ ] **Step 4: Run — expect PASS**, commit `feat: analytics service and endpoint`.

---

### Task 12: Dashboard page

**Files:**
- Create: `src/alay/api/routes/dashboard.py`, `src/alay/api/templates/dashboard.html`; Modify: `src/alay/api/main.py`
- Test: `tests/integration/test_dashboard.py`

**Interfaces:**
- Consumes: `/analytics`, `/checks?limit=20`, `/slot-events?limit=20` (fetched client-side).
- Produces: `GET /dashboard` → HTML. Chart.js from `https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js`. Content per §9 Dashboard: Live/Demo toggle (`?source=`), banner text `Демонстрационные данные` visible only in demo, cards for 24h/7d/30d (checks, success rate, errors, avg duration; `—` when `insufficient_data`), stacked bar of `timeline[].by_status`, tables of recent checks and slot events (evidence link when present), limitations block. Auto-refresh every 30 s.

- [ ] **Step 1: Write failing tests**

```python
async def test_dashboard_renders(client):      200, text/html, contains "chart.js@4.4.1", "/analytics"
async def test_demo_banner_only_in_demo(client): "?source=demo" contains banner element without hidden attr; default has it hidden
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement.** Manual check in browser with seeded demo data from a running worker.
- [ ] **Step 4: Run — expect PASS**, commit `feat: analytics dashboard`.

---

### Task 13: Docker packaging and full compose

**Files:**
- Create: `Dockerfile`, `.dockerignore`; Modify: `docker-compose.yml`, `adapters.yaml`

**Interfaces:**
- Produces compose services: `postgres` (Task 1), `migrate` (`sh -c "alembic upgrade head && python -m alay.sync_adapters"`, `restart: "no"`), `api` (`uvicorn alay.api.main:app --host 0.0.0.0 --port 8000`, port `8000:8000`, depends on `migrate` completed), `worker` (`python -m alay.worker`, depends on `migrate` completed and `mock-vfs` started), `mock-vfs` (`uvicorn alay.mock_vfs.app:app --host 0.0.0.0 --port 8081`, port `8081:8081`), `tests` (profile `tests`, `pytest`, `TEST_DATABASE_URL` → `postgres:5432/alay_test`). Shared volume `evidence` mounted at `/data/evidence` in `api` and `worker`. `api.main` exposes module-level `app = create_app()`.
- Dockerfile: `FROM mcr.microsoft.com/playwright/python:v<X>-noble` where `<X>` equals the `playwright` version locked in `uv.lock` (pin `playwright==<X>` in pyproject); copy `uv` from `ghcr.io/astral-sh/uv`, `uv sync --frozen --no-dev` (tests service image built with `--target test` stage including dev deps).
- Final `adapters.yaml` per §10: live Istanbul (60 min); `mock-istanbul-tourism` (`source: demo`, `start_url: http://mock-vfs:8081/rotate/login`, `login: true`, interval 1); `mock-<scenario>` for `available, no-slots, waitlist, captcha, login-wall (login: false), server-error, slow, changed-layout` with `enabled: false`.

- [ ] **Step 1: Build and start** — `docker compose up -d --build`. Expected: `migrate` exits 0; `api`, `worker`, `mock-vfs`, `postgres` running.
- [ ] **Step 2: Smoke** — `curl localhost:8000/health` → `"status":"ok"`; `curl -X POST "localhost:8000/adapters/mock-available/check?wait=true"` → 200, `"status":"AVAILABLE"`, `"source":"demo"`; `curl -X POST "localhost:8000/adapters/vfs-tur-fra-istanbul-tourism/check?wait=true"` → 200 with `CAPTCHA_REQUIRED` (or another honest status — record what it is); after 3 minutes `GET /checks?adapter_id=mock-istanbul-tourism` has ≥ 2 rows.
- [ ] **Step 3: Tests in container** — `docker compose --profile tests run --rm tests` → all pass.
- [ ] **Step 4: Scale** — `docker compose up -d --scale worker=2`; after 2 minutes no adapter has two checks with the same `job_id` (unique constraint holds) and `GET /health` shows `workers_alive: 2`.
- [ ] **Step 5: Commit** `feat: docker packaging and compose stack`.

---

### Task 14: Documentation and live evidence

**Files:**
- Create: `README.md`, `LIMITATIONS.md`, `docs/examples/*.json` (real responses captured from the running stack), `docs/examples/live-captcha.png` (copied evidence screenshot)

**Interfaces:**
- Consumes: the running stack from Task 13.

- [ ] **Step 1: Capture examples** — save responses of `GET /adapters`, `POST …/check?wait=true` (live and mock-available), `GET /checks?limit=5`, `GET /availability`, `GET /slot-events`, `GET /analytics?source=live`, `GET /analytics?source=demo`, `GET /health` into `docs/examples/`. Use only real outputs.
- [ ] **Step 2: README.md** — what it is (3 lines); quick start (`docker compose up -d --build`, URLs `/docs`, `/dashboard`, `localhost:8081`); architecture diagram (ASCII from spec §3) and data flow; tables summary; how to add a centre (YAML block + `docker compose run --rm migrate`) and an operator (adapter class skeleton); scaling (`--scale worker=N`, when to move to a broker); running tests locally and in Docker; demo script from spec §14; links to examples.
- [ ] **Step 3: LIMITATIONS.md** — observations table from spec §2 with the date and the actual live result from Step 1; what is not done and why (CAPTCHA bypass, automated login, ToS); what mock-vfs proves and does not prove; next integration steps (official API/partnership, operator-assisted session, notifications from `slot_events`, broker at scale).
- [ ] **Step 4: Verify** — every command in README run once from a clean `docker compose down -v`; all pass. Commit `docs: README, limitations, API examples`.
