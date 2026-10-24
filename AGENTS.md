# Kaloscope Repository Guide

This guide applies to the entire repository. Paths are relative to the repository root; run commands from the directory
shown. Read any more specific `AGENTS.md` before editing a subtree.

## Working approach

- Start with `git status --short --branch` and inspect the relevant implementation, callers, and tests. Preserve existing
  user changes and keep the diff limited to the requested work.
- Reuse existing domain modules, utilities, dependencies, and development environments. Make only the changes and small
  cleanups needed for the task; propose unrelated refactors separately. Format only files touched by the task.
- For a clear bug or small feature, investigate, implement, and verify directly. Ask only for missing business information
  or a necessary scope change, after checking available context. Continue independent work while awaiting an answer.
- For an unclear new module or major design, agree on scope and a concise design before implementation. Reuse an already
  approved design instead of restarting discovery.

## Architecture and entry points

Kaloscope manages a local media library through visual workflows. The frontend uses Svelte 5, SvelteKit 2, TypeScript,
Tailwind CSS 4, and DaisyUI 5. The backend uses Sanic, Pydantic 2, Tortoise ORM, and SQLite.

The frontend is an SPA: `frontend/src/routes/+layout.ts` disables SSR, and `frontend/svelte.config.js` enables runes and
static output in `frontend/build` with a `404.html` fallback. Production serves these assets and `/_api` through
`backend/app/main.py`. Keep server-side business logic in the Python backend.

| Location                                                                       | Responsibility                                                                                                                   |
| ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `frontend/src/routes`                                                          | Login, setup, and the dashboard, search, library, download, and settings pages under `(app)`                                     |
| `frontend/src/lib/components`                                                  | Reusable UI in `common`; domain components in `business`                                                                         |
| `frontend/src/lib/helpers.ts`                                                  | Shared form constraints, loading state, and sort helpers                                                                         |
| `frontend/src/lib/utils`                                                       | Shared browser, media, text, date, URL, and other focused utilities                                                              |
| `frontend/src/lib`                                                             | API client in `api.ts`; API types in `types.d.ts`; display enums in `enums.ts`; shared state in `stores.ts`                      |
| `frontend/src/lib/i18n.ts`, `frontend/src/lib/locales`                         | Translation helpers and the `en-US.json` / `zh-CN.json` catalogs                                                                 |
| `backend/app/routes`, `backend/app/services`, `backend/app/models`             | HTTP endpoints, reusable domain logic, ORM models, and Pydantic request models                                                   |
| `backend/app/utils`                                                            | Shared parsing, serialization, filesystem, async, and other utility functions                                                    |
| `backend/app/main.py`, `backend/app/core/config.py`, `backend/app/config.toml` | Lifecycle, process-shared state, configuration, and workspace paths                                                              |
| `backend/app/core/flow`, `backend/app/core/renderer.py`                        | Workflow engine, context, nodes, fields, edges, and template rendering                                                           |
| `backend/app/core/dl`                                                          | Downloader contracts, configuration, synchronization, and the `rpc` / `openlist` drivers                                         |
| `backend/app/core/media`, `backend/app/core/transcode`                         | Library monitoring, ingestion, metadata, FFmpeg transcoding, and hardware acceleration                                           |
| `frontend/src/lib/components/common/media`                                     | Video, image, and text viewers                                                                                                   |
| `backend/static/flow`, `backend/static`                                        | Shared node examples/references; NFO templates in `backend/static/templates`; downloader presets in `backend/static/downloaders` |
| `backend/app/migrations`, `backend/tests`, `scripts`                           | Database migrations, Pytest tests, and maintenance scripts                                                                       |

Runtime data lives in the root `workspace` directory locally and `/workspace` in containers. It includes the database,
secret key, caches, transcoded media, and workflow repositories. Keep runtime data out of source changes and use temporary
files and databases for tests.

## Environment and commands

Use Git, Python `>=3.13,<4.0`, Node.js 24+, Poetry 2.4+, and pnpm 11. Check `backend/pyproject.toml`,
`frontend/package.json`, `frontend/pnpm-workspace.yaml`, `Dockerfile`, and `.pre-commit-config.yaml` for the current
dependency and tooling configuration. `backend/poetry.toml` keeps the Python environment in `backend/.venv`.

| Directory       | Command                                                 | Purpose                                                    |
| --------------- | ------------------------------------------------------- | ---------------------------------------------------------- |
| `frontend`      | `pnpm install --frozen-lockfile`                        | Install the locked frontend dependencies when needed       |
| `backend`       | `poetry install`                                        | Install backend and development dependencies when needed   |
| `frontend`      | `pnpm run dev`                                          | Start Vite; `/_api` is proxied to `http://127.0.0.1:8000`  |
| `backend`       | `poetry run sanic app.main:app --fast --reload --debug` | Start the backend development server                       |
| `frontend`      | `pnpm run check`                                        | Run Svelte and TypeScript checks                           |
| `frontend`      | `pnpm run lint`                                         | Check Prettier formatting and ESLint rules                 |
| `frontend`      | `pnpm run build`                                        | Verify the static frontend and PWA build                   |
| `backend`       | `poetry run pytest tests/test_core_renderer.py -q`      | Run focused tests; substitute the affected test files      |
| `backend`       | `poetry run pytest -v`                                  | Run the full backend test suite                            |
| `backend`       | `poetry run ruff check .`                               | Check Python lint rules                                    |
| `backend`       | `poetry run ruff format --check .`                      | Check Python formatting without rewriting files            |
| Repository root | `git diff --check`                                      | Check the patch for whitespace errors and conflict markers |

- Frontend and backend development servers run separately. Backend startup applies migrations and starts workflows,
  library watchers, and download synchronization. Do not start the real service as a substitute for unit tests.
- Transcoding and embedded subtitles require `ffmpeg` and `ffprobe`. Executable lookup is implemented in
  `backend/app/core/transcode/transcoder.py` and `backend/app/services/subtitle.py`.
- Update dependency manifests and their matching `frontend/pnpm-lock.yaml` or `backend/poetry.lock` together. Use pnpm or
  Poetry to regenerate locks; preserve the policies and overrides in `frontend/pnpm-workspace.yaml`.
- For targeted formatting, run `pnpm exec prettier --write <path>` from `frontend` or
  `poetry run ruff format <path>` from `backend`. Quote shell paths containing `(app)`, `[lib_id]`, or similar characters.

## Implementation conventions

### Frontend and API contracts

- Use Svelte 5 runes (`$props`, `$state`, `$derived`, `$effect`) and existing stores. Reuse `$lib/components`,
  `$lib/helpers`, `$lib/utils`, and the existing Tailwind/DaisyUI patterns. Search focused utility modules before adding
  duplicate helpers or a new shared abstraction.
- Send normal application requests through the `api` client in `$lib/api` to preserve token injection, timeouts, retries,
  and common error handling. When changing API fields or states, check `frontend/src/lib/types.d.ts`,
  `frontend/src/lib/enums.ts`, and all affected callers together.
- `backend/app/core/middleware.py` wraps `/_api` JSON and plain-text responses with status `200` as
  `{ request_id, status, message, data }`. Routes return business data; clients read `Resp<T>`. Do not wrap responses twice
  or change the established handling of empty, file, and streaming responses.
- Translate user-visible text through `$lib/i18n` and update both locale catalogs. New backend `ErrorCode` values also
  require matching messages in both languages.
- Add endpoints using Sanic `Blueprint` and `@validate` request models, delegating reusable logic to domain services.
  `backend/app/utils/importer.py` discovers blueprints. `BaseService` uses class methods and must not be instantiated.
- Preserve backend authorization: `on_request` handles login, while `@authorize` and domain filters handle roles and
  resources. Hiding a frontend control is not an authorization check. Authentication exclusions are configured through
  `auth_exclude_paths` in `backend/app/config.toml`.

### Workflows, downloads, and background tasks

- Base new workflow nodes on `Node` in `backend/app/core/flow/nodes/base.py` and the existing category decorators.
  Preserve node types, field IDs, and edge handles used by saved workflows. Existing categories use `import_subclasses`;
  new categories also need an import in `backend/app/core/flow/nodes/__init__.py`.
- For a new field type, update the implementation and exports in `backend/app/core/flow/fields` and the component exports
  in `frontend/src/lib/components/business/flow/designer/fields/index.ts`. The backend `field_type` must match the frontend
  export name. Check node translations, frontend types, and the relevant JSONC examples as well.
- Keep downloader behavior aligned with `DownloaderDriver`, `DownloadState`, and `DownloadAction` in
  `backend/app/core/dl/driver.py`. Register new drivers in the configuration and driver factories in
  `backend/app/core/dl/config.py`, and update `backend/static/downloaders`. State or capability changes also affect
  services, synchronization, frontend enums, and controls gated by `capabilities`.
- Reuse the `shared_ctx`, locks, queues, and lifecycle hooks in `backend/app/main.py` for state shared across workers.
  Preserve task cancellation and resource cleanup. Delayed download actions follow the `DLSyncer.publish` path.
- Reuse application HTTP clients and `backend/app/core/network.py` for ordinary external requests to retain proxy, DNS,
  cookie, and cache behavior. Keep specialized clients within their existing lifecycle management.

### Models and migrations

- Keep ORM and Pydantic request models in the relevant `backend/app/models` domain module. Reuse `TortoiseModel`,
  `Pageable`, and the other existing base models.
- Use Tortoise's built-in migrations. `backend/pyproject.toml` points to `app.main.tortoise_config`; after a persistent
  schema change, run `poetry run tortoise makemigrations --name <descriptive_name>` from `backend` and review the generated
  files in `backend/app/migrations`. Always use a concise, descriptive custom name in `snake_case`. Startup applies
  migrations through `upgrade_db`.
- Before a release, consolidate all schema changes for that release into a single unreleased migration. Update or
  regenerate that migration instead of adding another one. Start a new migration for the next release only after the
  current one has been released.
- Never rewrite a released migration. Review defaults, enum values, indexes, and compatibility with existing data.
  Keep behavioral coverage in model and service tests; do not add dedicated tests for migration files.

## Code and documentation style

Follow `frontend/.prettierrc` and `frontend/eslint.config.js` for the frontend. Use PascalCase for Svelte components and
SvelteKit's route naming conventions. Python formatting and lint rules live in `backend/pyproject.toml`; migrations are
excluded from the normal Ruff checks.

### Function and method documentation

These requirements apply to new or modified business functions, utilities, and methods, including private helpers that
start with `_`. They take precedence over inconsistent comments in existing modules. Update only functions touched by
the task; test functions follow the separate rules below.

- Use Markdown single backticks around identifiers, code, commands, paths, and literals in comments and docstrings.
  Do not use reStructuredText double backticks.
- Python docstrings must use English Google style. Start with an imperative summary, capitalized and ending in a period.
  Separate details and sections from the summary with a blank line; capitalize descriptions and end them with periods.
- Include `Args:` when a Python function takes parameters, excluding `self` and `cls`; include `Returns:` when it returns
  a value and `Raises:` for explicit exceptions relevant to callers. Explain meaning and important boundary behavior,
  omit empty sections, and do not repeat signature types. A summary alone does not replace required sections.
- Use English JSDoc (`/** ... */`) for TypeScript, JavaScript, and Svelte functions. Start the summary with a capital
  letter and end it with a period. Document parameters as `@param name - Description.` and returned values as
  `@returns Description.`, keeping documentation aligned with the signature.
- Single-line comments inside function or method bodies start with a lowercase letter and have no final period.

### Unit tests

- Put backend tests in `backend/tests/test_*.py` and include a short English module docstring, such as
  `"""Unit tests for the flow engine."""`.
- Use short `test_` names describing an object or scenario: `test_event_trigger`, `test_auth_log`, or `test_search_log`.
  Let the file or class supply context; avoid repeating the full function name or listing multiple assertions in a name.
- Preserve the target file's module-level or `TestXxx` class organization. Within an existing class, prefer short scenario
  names such as `test_default` and `test_missing`; do not reorganize unrelated tests just to shorten names.
- Separate setup, execution, and assertions with blank lines where useful. Do not add redundant phase comments or apply
  the business-function docstring template to test functions. Parameterize equivalent cases with
  `pytest.mark.parametrize`; reuse existing fixtures or small local helpers for shared setup.
- Cover observable behavior, boundaries, and error recovery when changing utilities, renderers, workflows, or services.
  Reuse adequate existing coverage; add focused regression tests for uncovered behavior instead of mirroring implementation
  details. Do not add a test framework or require TDD by default.
- Follow the existing async test pattern: a synchronous test calls `asyncio.run()`. Use `tmp_path`, `monkeypatch`,
  `AsyncMock`, and `httpx.MockTransport` to isolate files and external services. ORM tests use in-memory or temporary SQLite
  and close connections after use.

## Generated files and maintenance

Update generated files through their sources and scripts, not by editing output directly.

| Source or task                                       | Update procedure                                                                                                                                          |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `frontend/src/lib/icons/icons.ts`                    | From `frontend`, run `pnpm run build:icons` to update `frontend/src/lib/icons/$icons.ts`; new icons may require Iconify API access                        |
| `frontend/src/lib/grammars/jsonc.grammar`            | From `frontend`, run `pnpm run build:jsonc` to regenerate `jsonc-parser.js` and `jsonc-parser.terms.js` in the same directory                             |
| Frontend source, static assets, or PWA configuration | Use `pnpm run build` when required by the validation matrix; do not maintain `frontend/build` or `frontend/.svelte-kit` as source                         |
| Release versions                                     | From the repository root, run `python3 scripts/bump_version.py patch` (or `minor` / `major`) to update both project manifests when a release is requested |

## Validation and delivery

Choose checks according to the affected behavior. Complete the applicable checks below, then broaden or repeat them only
when new changes, failures, or unresolved concerns justify it.

| Change                                                              | Required verification                                                                                                        |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Documentation only                                                  | Verify referenced paths, commands, Markdown formatting, and `git diff --check`; application tests and builds are unnecessary |
| Backend behavior                                                    | Run focused Pytest tests and both Ruff checks from the command table                                                         |
| Shared backend infrastructure, models, or behavior spanning domains | Also run the full backend test suite                                                                                         |
| Persistent schema                                                   | Review migration operations and data compatibility; verify affected model and service behavior                               |
| Frontend code                                                       | Run `pnpm run check` and `pnpm run lint`                                                                                     |
| Frontend routes, build configuration, dependencies, or PWA          | Also run `pnpm run build`                                                                                                    |
| UI behavior or layout                                               | Inspect the affected states in the browser when available; include screenshots in a PR                                       |

- No frontend test framework is currently configured. Use the existing checks and targeted browser verification; add
  component tests only after a framework has been introduced.
- `.github/workflows/release.yml` publishes images on version tags; it does not provide general test or lint jobs.
  Perform the applicable local checks instead of assuming CI will cover them.
- To install hooks, run `poetry run prek install` from `backend`. `.pre-commit-config.yaml` includes Commitizen, Poetry,
  Ruff, Prettier, and file checks. Dependency, lockfile, and formatting hooks can write files, and the Prettier hook also
  targets the frontend directory. Inspect the diff after hooks run and keep unrelated changes out of the commit.
- Before delivery, review the final diff and Git status. Report what changed, which checks actually ran, and any failures
  or unverified areas. Never describe a skipped or blocked check as passing.
- Do not commit or push unless the user requests it. After feature or bug-fix work, suggest an English Conventional
  Commit: `<type>(<scope>): <description>`. The scope is optional; keep the description at most 50 characters with no final
  period. Allowed types are `feat`, `fix`, `build`, `bump`, `chore`, `ci`, `docs`, `perf`, `refactor`, `revert`, `style`, and
  `test`. Add a body only when the change needs an explanation of motivation or impact.
- Pull requests target `main` by default and cover one coherent change. Describe the resulting behavior, related issues,
  and verification commands; include screenshots for UI changes.
