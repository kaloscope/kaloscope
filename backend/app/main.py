import logging
import multiprocessing
import os
from multiprocessing.managers import SyncManager
from pathlib import Path

import httpx
from hishel import AsyncSqliteStorage
from hishel.httpx import AsyncCacheTransport
from sanic import Sanic
from sanic.log import Colors, logger
from tortoise import Tortoise
from tortoise.migrations.api import migrate

import app.routes as routes
from app.core.config import KaloscopeConfig
from app.core.constants import APP_NAME, URL_PREFIX
from app.core.cookiejar import SQLiteCookieJar
from app.core.dl.syncer import DLSyncer
from app.core.exceptions import error_handler
from app.core.flow.engine import FlowEngine
from app.core.media.watcher import LibWatcher
from app.core.middleware import SessionHolder, on_request, on_response
from app.core.monkeypatch import apply_monkey_patches
from app.core.network import NetworkTransport
from app.utils.importer import register_blueprints
from app.utils.json import dumps, loads

apply_monkey_patches()

# get the root path of the project
APP_PATH = Path(__file__).resolve().parent
ROOT_PATH = APP_PATH.parents[1]

# load the configuration file
app_config = KaloscopeConfig(
    APP_PATH / "config.toml",
    None if os.environ.get("CONTAINER") == "true" else ROOT_PATH,
)
tortoise_config = app_config.tortoise

# create the Sanic app
app = Sanic(APP_NAME, log_config=app_config.logging, dumps=dumps, loads=loads)
app_config.configure(app)
app.static("/", ROOT_PATH / "frontend/build", index="index.html")
app.register_middleware(on_request, "request")
app.register_middleware(on_response, "response")
app.error_handler.add(Exception, error_handler)
register_blueprints(app, routes, url_prefix=URL_PREFIX)


@app.main_process_start
async def main_process_start(app: Sanic):
    """Handle the main process startup event."""
    await init_shared_ctx(app)
    await init_workspace(app)
    await upgrade_db(app)


@app.before_server_start
async def before_server_start(app: Sanic):
    """Handle the worker process startup event."""
    app.loop.set_debug(False)
    await start_orm(app)
    await start_http_client(app)
    await start_flow_engine(app)
    await start_lib_watcher(app)
    await start_dl_syncer(app)
    await load_sessions(app)


@app.before_server_stop
async def before_server_stop(app: Sanic):
    """Handle the worker process shutdown event."""
    await save_sessions(app)
    await close_dl_syncer(app)
    await close_lib_watcher(app)
    await close_flow_engine(app)
    await close_http_client(app)
    await close_orm(app)


@app.main_process_stop
async def main_process_stop(app: Sanic):
    """Handle the main process shutdown event."""
    await close_shared_ctx(app)


async def init_shared_ctx(app: Sanic):
    """Initialize the shared context.

    See https://sanic.dev/en/guide/running/manager.html for more details.
    """
    shared = app.shared_ctx
    app.ctx.sync_manager = (sync_manager := multiprocessing.Manager())
    # shared online user sessions
    shared.sessions = sync_manager.dict()
    shared.sessions_lock = multiprocessing.Lock()
    # shared objects for the HTTP client
    shared.cookies = sync_manager.dict()
    shared.cookies_lock = multiprocessing.RLock()
    shared.csrf_tokens = sync_manager.dict()
    # shared objects for the flow engine
    shared.flow_lock = multiprocessing.Lock()
    shared.flow_events = multiprocessing.Queue()
    shared.flow_reload_flag = multiprocessing.Event()
    shared.flow_job_actions = sync_manager.dict()
    shared.flow_running_tasks = sync_manager.list()
    shared.repo_fetcher_flag = multiprocessing.Event()
    # shared objects for the media library watcher
    shared.lib_watcher_lock = multiprocessing.Lock()
    shared.lib_watcher_actions = sync_manager.dict()
    shared.lib_scanning_paths = sync_manager.list()
    shared.lib_observing_paths = sync_manager.list()
    # shared objects for the download synchronizer
    shared.dl_syncer_lock = multiprocessing.Lock()
    shared.dl_syncer_flag = multiprocessing.Event()
    shared.dl_sync_fast = multiprocessing.Event()
    # shared objects for transcoding task monitoring
    shared.transcode_tasks = sync_manager.dict()
    shared.transcode_tasks_lock = multiprocessing.Lock()
    logger.debug(_msg(Colors.BLUE, "Shared context initialized:\n"), shared)


async def close_shared_ctx(app: Sanic):
    """Shutdown the shared context manager."""
    sync_manager: SyncManager = app.ctx.sync_manager
    sync_manager.shutdown()
    logger.debug(_msg(Colors.YELLOW, "Shared context manager shutdown."), _worker(app))


async def init_workspace(app: Sanic):
    """Initialize the workspace directories."""
    for path in app_config.workspace.values():
        Path(path).mkdir(parents=True, exist_ok=True)
    logger.debug(_msg(Colors.BLUE, "Workspace initialized:\n"), app_config.workspace)


async def upgrade_db(app: Sanic):
    """Upgrade the database schema.

    See https://tortoise.github.io/migration.html for more details.
    """
    await migrate(config=tortoise_config)
    await Tortoise.close_connections()
    logger.debug(_msg(Colors.BLUE, "Database schema upgraded."), _worker(app))


async def start_orm(app: Sanic):
    """Initialize the Tortoise ORM.

    See https://tortoise.github.io/examples/sanic.html for more details.
    """
    if app.debug:
        # enable debug logging for Tortoise ORM
        logging.getLogger("tortoise").setLevel(logging.DEBUG)
        db_client_logger = logging.getLogger("tortoise.db_client")
        db_client_logger.setLevel(logging.DEBUG)
        # suppress noisy SQL statements (INSERT/UPDATE on large tables, etc.)
        sql_noise = [
            'INSERT INTO "flow_log"',
        ]
        db_client_logger.addFilter(
            lambda r: not any(r.getMessage().startswith(s) for s in sql_noise)
        )
    await Tortoise.init(tortoise_config, _enable_global_fallback=True)
    logger.debug(_msg(Colors.BLUE, "Tortoise ORM initialized."), _worker(app))


async def close_orm(app: Sanic):
    """Close the Tortoise ORM connections."""
    await Tortoise.close_connections()
    logger.debug(_msg(Colors.YELLOW, "Tortoise ORM closed."), _worker(app))


async def start_http_client(app: Sanic):
    """Initialize the HTTP client.

    See https://www.python-httpx.org/async/ for more details.
    """
    if app.debug:
        # enable debug logging for HTTPX
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
    app.ctx.cookies = SQLiteCookieJar(app)
    await app.ctx.cookies.load()
    app.ctx.httpx = httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        cookies=app.ctx.cookies,
        transport=AsyncCacheTransport(
            next_transport=NetworkTransport(http2=True),
            storage=AsyncSqliteStorage(database_path=_cache_path(app)),
        ),
    )
    logger.debug(_msg(Colors.BLUE, "HTTP client initialized."), _worker(app))


async def close_http_client(app: Sanic):
    """Close the HTTP client."""
    client: httpx.AsyncClient = app.ctx.httpx
    await client.aclose()
    cookies: SQLiteCookieJar = app.ctx.cookies
    await cookies.save()
    logger.debug(_msg(Colors.YELLOW, "HTTP client closed."), _worker(app))


async def start_flow_engine(app: Sanic):
    """Start the flow engine."""
    app.ctx.flow_engine = FlowEngine(app)
    await app.ctx.flow_engine.start()
    logger.debug(_msg(Colors.BLUE, "Flow engine started."), _worker(app))


async def close_flow_engine(app: Sanic):
    """Shutdown the flow engine."""
    engine: FlowEngine = app.ctx.flow_engine
    await engine.shutdown()
    logger.debug(_msg(Colors.YELLOW, "Flow engine shutdown."), _worker(app))


async def start_lib_watcher(app: Sanic):
    """Start the media library watcher."""
    app.ctx.lib_watcher = LibWatcher(app)
    await app.ctx.lib_watcher.start()
    logger.debug(_msg(Colors.BLUE, "Media library watcher started."), _worker(app))


async def close_lib_watcher(app: Sanic):
    """Shutdown the media library watcher."""
    watcher: LibWatcher = app.ctx.lib_watcher
    await watcher.shutdown()
    logger.debug(_msg(Colors.YELLOW, "Media library watcher shutdown."), _worker(app))


async def start_dl_syncer(app: Sanic):
    """Start the download synchronizer."""
    app.ctx.dl_syncer = DLSyncer(app)
    await app.ctx.dl_syncer.start()
    logger.debug(_msg(Colors.BLUE, "Download synchronizer started."), _worker(app))


async def close_dl_syncer(app: Sanic):
    """Shutdown the download synchronizer."""
    syncer: DLSyncer = app.ctx.dl_syncer
    await syncer.shutdown()
    logger.debug(_msg(Colors.YELLOW, "Download synchronizer shutdown."), _worker(app))


async def load_sessions(app: Sanic):
    """Load user sessions from the database."""
    app.ctx.sessions = SessionHolder(app)
    await app.ctx.sessions.load()
    logger.debug(_msg(Colors.BLUE, "User sessions loaded."), _worker(app))


async def save_sessions(app: Sanic):
    """Save user sessions to the database."""
    sessions: SessionHolder = app.ctx.sessions
    await sessions.save()
    logger.debug(_msg(Colors.YELLOW, "User sessions saved."), _worker(app))


def _msg(color: str, text: str) -> str:
    """Generate a formatted log message.

    Args:
        color: The color of the message.
        text: The text of the message.

    Returns:
        The formatted log message.
    """
    return f"{color}{text} {Colors.BOLD}{Colors.SANIC}[%s]{Colors.END}"


def _worker(app: Sanic) -> str:
    """Get the worker name.

    Args:
        app: The Sanic application instance.

    Returns:
        The worker name.
    """
    return app.m.name if hasattr(app, "m") else "main"


def _cache_path(app: Sanic) -> Path:
    """Get the worker-specific cache database path.

    Args:
        app: The Sanic application instance.

    Returns:
        The cache database path.
    """
    worker = _worker(app)
    return Path(app_config.get_workspace("cache")) / f"{worker}.db"
