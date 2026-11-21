# Kaloscope Repository Guide

This guide applies to the entire repository. Paths are relative to the repository root; run commands from the directory
shown. Read any more specific `AGENTS.md` before editing a subtree.

## Working approach

- **Change the frontend UI only when the user explicitly requests or approves the specific change.** This includes
  pages, layout, styling, controls, copy, visible defaults, navigation, and interaction behavior, including changes made
  through shared components or backend responses. If assistant, MCP, or backend work calls for an additional UI change,
  describe the affected screen and proposed behavior and get approval before implementing it.
- Start with `git status --short --branch` and inspect the relevant implementation, callers, and tests. Preserve existing
  user changes and keep the diff limited to the requested work.
- Reuse existing domain modules, utilities, dependencies, and development environments. Make only the changes and small
  cleanups needed for the task; propose unrelated refactors separately. Format only files touched by the task.
- For a clear bug or small feature, investigate, implement, and verify directly. Ask only for missing business information
  or a necessary scope change, after checking available context. Continue independent work while awaiting an answer.
- For an unclear new module or major design, agree on scope and a concise design before implementation. Reuse an already
  approved design instead of restarting discovery.

## Architecture and entry points

Kaloscope manages a local media library through visual workflows, with an AI assistant and an MCP server built on the
same permission-checked tools. The frontend uses Svelte 5, SvelteKit 3, Vite 8, TypeScript 6, Tailwind CSS 4, and
DaisyUI 5. The backend uses Sanic, Pydantic 2, Tortoise ORM, and SQLite.

The frontend is an SPA: `frontend/src/routes/+layout.ts` disables SSR. There is no `svelte.config.js`; the SvelteKit
options (static adapter output in `frontend/build` with a `404.html` fallback, runes, and the PWA service worker) are
passed to `withPwa(...)` in `frontend/vite.config.ts`. Production serves these assets and `/_api` through
`backend/app/main.py`. Keep server-side business logic in the Python backend.

| Location                                                                          | Responsibility                                                                                                                                                           |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `frontend/src/routes`                                                             | `login` and `setup`; `dashboard`, `websearch`, `medialibs`, `downloads` (admin only), and `settings` under `(app)`                                                       |
| `frontend/src/lib/components`                                                     | Reusable UI in `common` (media viewers in `common/media`); domain components in `business`                                                                               |
| `frontend/src/lib`                                                                | API client in `api.ts`; API types in `types.d.ts`; display enums in `enums.ts`; shared rune state in `stores.svelte.ts`; form, loading, and sort helpers in `helpers.ts` |
| `frontend/src/lib/utils`                                                          | Focused browser, media, text, date, number, URL, YAML, and timing utilities                                                                                              |
| `frontend/src/lib/i18n.ts`, `frontend/src/lib/locales`                            | Translation helpers and the `en-US.json` / `zh-CN.json` catalogs                                                                                                         |
| `frontend/src/lib/assistant`, `frontend/src/pwa`                                  | Assistant session, streaming, and canvas state; the PWA service worker with its own `tsconfig.json`                                                                      |
| `backend/app/routes`, `backend/app/services`, `backend/app/models`                | HTTP endpoints, reusable domain logic, ORM models, and Pydantic request models                                                                                           |
| `backend/app/utils`                                                               | Shared parsing, serialization, filesystem, proxy, async, and other utility functions                                                                                     |
| `backend/app/main.py`, `backend/app/core/config.py`, `backend/app/config.toml`    | Lifecycle, process-shared state, configuration, assistant budgets, and workspace paths                                                                                   |
| `backend/app/core/middleware.py`, `backend/app/core/decorators.py`                | Login sessions, MCP token authentication, response wrapping, and the `@authorize` role check                                                                             |
| `backend/app/core/flow`, `backend/app/core/renderer.py`                           | Workflow engine, context, nodes, fields, edges, validation, and Jinja template rendering                                                                                 |
| `backend/app/core/ai`, `backend/app/services/assistant*.py`, `backend/app/mcp.py` | AI providers, assistant tools, context history, proposals, and the MCP server at `/_api/mcp`                                                                             |
| `backend/app/core/dl`                                                             | Downloader contracts, configuration, synchronization, and the `rpc` / `openlist` drivers                                                                                 |
| `backend/app/core/media`, `backend/app/core/transcode`                            | Library monitoring, ingestion, organization, batches, metadata, FFmpeg transcoding, and hardware acceleration                                                            |
| `backend/static`                                                                  | Workflow authoring references read by `backend/app/core/flow/reference.py` in `flow`; NFO templates in `templates`; downloader presets in `downloaders`                  |
| `backend/app/migrations`, `backend/tests`, `scripts`                              | Database migrations, Pytest tests, and maintenance scripts                                                                                                               |

Runtime data lives in the root `workspace` directory locally and `/workspace` in containers. It includes the database,
secret key, caches, transcoded media, and workflow repositories. Keep runtime data out of source changes and use temporary
files and databases for tests.

## Environment and commands

Use Git, Python `>=3.13,<4.0`, Node.js 24+, Poetry 2.5+, and pnpm 11 (the `Dockerfile` installs `pnpm@latest-11`).
Check `backend/pyproject.toml`, `frontend/package.json`, `frontend/pnpm-workspace.yaml`, `Dockerfile`, and
`.pre-commit-config.yaml` for the current dependency and tooling configuration. `backend/poetry.toml` keeps the Python
environment in `backend/.venv`. Confirm `node --version` first; an older default Node breaks the frontend tooling.

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
| `backend`       | `poetry run pytest -q`                                  | Run the full backend suite (about 10 minutes)              |
| `backend`       | `poetry run ruff check .`                               | Check Python lint rules                                    |
| `backend`       | `poetry run ruff format --check .`                      | Check Python formatting without rewriting files            |
| Repository root | `git diff --check`                                      | Check the patch for whitespace errors and conflict markers |

- Frontend and backend development servers run separately. Backend startup applies migrations and starts workflows,
  library watchers, and download synchronization. Do not start the real service as a substitute for unit tests.
- Transcoding and embedded subtitles require `ffmpeg` and `ffprobe`. Executable lookup is implemented in
  `backend/app/core/transcode/transcoder.py` and `backend/app/services/subtitle.py`.
- Update dependency manifests and their matching `frontend/pnpm-lock.yaml` or `backend/poetry.lock` together. Use pnpm or
  Poetry to regenerate locks; preserve the policies and overrides in `frontend/pnpm-workspace.yaml`. Its
  `minimumReleaseAge: 1440` rejects package versions published less than 24 hours ago; wait instead of bypassing it.
- `pnpm run <script>` can install dependencies first when `frontend/node_modules` differs from the lockfile. When only
  checking code in that state, run the script's binaries from `frontend/node_modules/.bin` directly. Never run pnpm in a
  copied tree that links to the real `node_modules`.
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
  `backend/app/utils/importer.py` discovers blueprints under `/_api`. `BaseService` uses class methods and must not be
  instantiated.

### Authorization and untrusted input

- `on_request` in `backend/app/core/middleware.py` requires a login session for every `/_api` path except
  `auth_exclude_paths` in `backend/app/config.toml`; `/_api/mcp` accepts only MCP access tokens. A route without
  `@authorize` is therefore open to every signed-in user, including the `user` role.
- Put `@authorize(role=UserRole.ADMIN)` above `@validate` on any route that changes shared state, executes code or
  templates, or exposes the server filesystem. Plain `@authorize()` loads `user.perms` for non-admin users; filter indexer
  and media-library data with it. Hiding a frontend control is not an authorization check.
- The session cookie also authenticates GET requests, including `<img>` and `<video>` sources and cross-site top-level
  navigations. Use POST for new state-changing endpoints, and never let GET parameters choose where files are written.
- Resolve user-supplied paths and require them to stay inside their intended root with `Path.is_relative_to`, as
  `backend/app/routes/image.py` and `serve_hls_file` in `backend/app/routes/media.py` do. Joining an absolute path or `..`
  segments onto a root escapes it.
- The default `render(...)` environment exposes Python internals for workflow execution. Render templates from assistant,
  MCP, or other non-admin input with `sandboxed=True`.
- Fetch remote URLs supplied by non-admin callers through `proxy_policy` and `proxy_stream` in
  `backend/app/utils/proxy.py`, which restrict every hop to public addresses. Boot parameters override workflow variables,
  so non-admin indexer runs keep only the `inputs` each start node declares (`FlowGraphService.caller_params`); declare
  new caller inputs there.
- Treat scraped results, NFO metadata, and media-file tags (titles, uploaders, chapters, track names) as hostile; the
  login token lives in `localStorage`, so any script injection takes over the session. Svelte text bindings escape them,
  but xgplayer plugin `render()` templates and option-list `showText`/`text` values are inserted as HTML: wrap such
  values with `escapeHTML` from `$lib/utils/text`. Avoid new `{@html}` and `innerHTML` uses.

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
- Assistant tools in `backend/app/core/ai/tools.py` and MCP tools in `backend/app/mcp.py` bypass route decorators. Mark
  admin-only assistant tools with `admin=True`, check roles inside MCP tools (for example with `_require_admin`), and apply
  the caller's resource permissions in both. The system prompt and tool schemas consume a fixed context budget:
  `test_floor_catalog` in `backend/tests/test_core_ai_tools.py` caps fixed text at 24,796 bytes per catalog state.
  Measure growth with `fixed_size(...)` and offset it instead of loosening assertions.

### Assistant interactions and MCP scopes

- Use `discover_actions` for role-aware tool discovery and keep newly unlocked schemas inside the catalog budget.
  Download grants live in `backend/app/core/dl/access.py`. Keep MCP discovery, call-time checks,
  scoped tool schemas, and historical resource checks consistent when adding actions. Require a verified token for both
  discovery and execution. Previews that run an external workflow, including media rescraping, require a read-write token.
- Use `ask_user` and the signed-in conversation's answer endpoint for missing values. Options, custom text, skipped
  questions, and retries carry no execution grant. Keep device actions tied to a validated target and browser acknowledgement.
- For administrator subscriptions, use one ordinary `ask_user` group. Offer release choices from actual search results and
  collect unresolved downloader directories and library destinations in the same group. The generic card's final submit
  confirms the subscription. Keep server-owned option bindings and resolve custom library names or IDs within the
  caller's current grants. Skipping a required question defers creation.
- Apply answers by question and option ID to the frozen plan. Recheck the exact keyword, filters, and transfer settings
  before creating. Use the shared creation receipt for HTTP and MCP retries. Keep earlier single-question cards readable
  and supersede a preliminary validation when a later question revises it.
- Reuse the domain service's request ID, revision, and receipt for browser approvals.
  `assistant_reviews.py` handles subscriptions, downloads, schedules, and media changes. Existing plan edits use
  `assistant_plan_review.py`. Rebuilds use `plan_replacement.py`, and other administrator reviews use `agent_actions.py`.
  Keep confirmation for plan rebuilds and administrator actions inside the assistant panel. MCP previews return an
  operation ID for status checks. Recheck the owner and role on every read and decision. Reserve the SQLite writer
  before reading a decision for update.
  Distinguish a retryable failure before execution from an uncertain outcome after dispatch.
- Validate these changes across chat, authenticated HTTP, and Streamable HTTP MCP. From `backend`, run
  `KALOSCOPE_LIVE_AGENT=1 poetry run pytest tests/test_agent*_live.py -q` for the opt-in model evaluations. They read the
  enabled official DeepSeek provider from the local workspace and use temporary databases, synthetic media, and loopback
  servers. Keep credentials out of traces and committed fixtures. Model tests use the provider's metered API.

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
  and close connections after use. Patch `KaloscopeConfig.get_workspace` so tests never touch the real `workspace`.
- `sanic-testing` is not installed. Route tests build a real `sanic.Request` for a test `Sanic` app registered in a
  fixture and call the decorated handler directly; see `backend/tests/test_routes_ai.py`. Cover anonymous and `user`
  roles for authorized routes, and unregister the app afterwards.
- Earlier test modules can leave Sanic apps registered, so `Sanic.get_app()` without a name may fail only in the full
  suite. Pass the app name, or patch `Sanic.get_app` in tests whose code path reaches it.

## Generated files and maintenance

Update generated files through their sources and scripts, not by editing output directly.

| Source or task                                       | Update procedure                                                                                                                                             |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `frontend/src/lib/icons/icons.ts`                    | From `frontend`, run `pnpm run build:icons` to update `frontend/src/lib/icons/$icons.ts`; new icons may require Iconify API access                           |
| `frontend/src/lib/grammars/jsonc.grammar`            | From `frontend`, run `pnpm run build:jsonc` to regenerate `jsonc-parser.js` and `jsonc-parser.terms.js` in the same directory                                |
| Frontend source, static assets, or PWA configuration | Use `pnpm run build` when required by the validation matrix; do not maintain `frontend/build` or `frontend/.svelte-kit` as source                            |
| Release versions                                     | From the repository root, run `python3 scripts/bump_version.py patch` (or `minor` / `major`) to update both project manifests when a release is requested    |
| `LICENSES.md` and the README license sections        | From the repository root, run `python3 scripts/scan_packages.py` after dependency changes; it calls `pnpm dlx license-checker` and `poetry run pip-licenses` |

## Validation and delivery

Choose checks according to the affected behavior. Complete the applicable checks below, then broaden or repeat them only
when new changes, failures, or unresolved concerns justify it.

| Change                                                              | Required verification                                                                                                        |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Documentation only                                                  | Verify referenced paths, commands, Markdown formatting, and `git diff --check`; application tests and builds are unnecessary |
| Backend behavior                                                    | Run focused Pytest tests and both Ruff checks from the command table                                                         |
| Authorization, file paths, or template rendering                    | Also add regression tests for the rejected roles and escaping inputs, and confirm they fail without the fix                  |
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
