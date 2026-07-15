import asyncio
import base64
import uuid
from fnmatch import fnmatch
from functools import cached_property
from multiprocessing.synchronize import Event, Lock
from pathlib import Path
from typing import Any

import aiofiles
from git import Git, GitError, InvalidGitRepositoryError
from git.repo import Repo
from sanic import Sanic
from sanic.log import Colors, logger
from sanic.request.form import File
from tortoise import timezone

from app.core.config import KaloscopeConfig
from app.core.constants import ENCODING
from app.models.flow import FlowRepository, FlowTemplate
from app.models.network import ProxyProtocol, URLRule
from app.utils import json
from app.utils.crypto import xor_decrypt

_GIT_SYNC_TIMEOUT = 30
"""The timeout for Git operations in seconds."""


class RepoFetcher:
    """The GitHub repository fetcher."""

    _REPO_FETCHER = "repo_fetcher"
    _INITIAL_SYNC_DELAY = 70

    def __init__(self, app: Sanic):
        """Initialize the repository fetcher.

        Args:
            app: The Sanic application instance.
        """
        self._app = app
        self._task = None
        # ensure that only one instance is running
        if self._fetcher_lock.acquire(block=False):
            try:
                if not self._fetcher_flag.is_set():
                    self._task = self._REPO_FETCHER
                    self._fetcher_flag.set()
            finally:
                self._fetcher_lock.release()

    @cached_property
    def _fetcher_lock(self) -> Lock:
        return self._app.shared_ctx.flow_lock

    @cached_property
    def _fetcher_flag(self) -> Event:
        return self._app.shared_ctx.repo_fetcher_flag

    async def start(self):
        """Start the repository fetcher."""
        if self._task:
            self._app.add_task(self.interval(), name=self._task)

    async def shutdown(self):
        """Shutdown the repository fetcher."""
        if self._task:
            self._fetcher_flag.clear()
            await self._app.cancel_task(self._task)

    async def interval(self):
        """Synchronize the flow repositories."""
        try:
            await asyncio.sleep(self._INITIAL_SYNC_DELAY)
            while True:
                try:
                    repositories = await FlowRepository.all()
                    for repo in repositories:
                        await fetch_origin(repo)

                    await asyncio.sleep(3600)
                except Exception:
                    logger.error(
                        "Failed to synchronize the repositories!", exc_info=True
                    )
                    # wait for 10 minutes before retrying
                    await asyncio.sleep(600)
        except asyncio.CancelledError:
            pass


async def _proxy_environment(url: str) -> dict[str, str]:
    """Build Git proxy environment variables for a repository URL.

    Args:
        url: The repository URL to match against the proxy rules.

    Returns:
        A dict of proxy environment variables for Git, or an empty dict.
    """
    proxy_rules = (
        await URLRule.filter(
            http_proxy=True,
            proxy_id__not_isnull=True,
            proxy__protocol=ProxyProtocol.HTTP,
        )
        .select_related("proxy")
        .order_by("priority")
    )

    for rule in proxy_rules:
        pattern = p if (p := rule.pattern).endswith("*") else p + "*"
        if not fnmatch(url, pattern) or not rule.proxy:
            continue

        proxy = rule.proxy
        if (username := proxy.username) and (password := proxy.password):
            password = xor_decrypt(password)
            proxy_url = f"http://{username}:{password}@{proxy.host}:{proxy.port}"
        else:
            proxy_url = f"http://{proxy.host}:{proxy.port}"

        logger.info(
            f"Using proxy {Colors.BLUE}%s{Colors.END} for repository:"
            f"{Colors.GREEN} %s{Colors.END}",
            proxy_url,
            url,
        )
        return {"HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url}

    return {}


async def fetch_origin(repo: FlowRepository):
    """Fetch the latest changes from the remote repository.

    Args:
        repo: The repository to synchronize.
    """
    repo_dir = Path(KaloscopeConfig.get_workspace("repositories"))
    repo_path = repo_dir / repo.repo_name
    repo_path.mkdir(parents=True, exist_ok=True)
    proxy_env = await _proxy_environment(repo.repo_url)
    try:
        try:
            # try to pull the latest changes from the remote repository
            await asyncio.to_thread(_pull_origin, repo_path, proxy_env)
            logger.info(
                f"Pulled latest changes for repository:{Colors.GREEN} %s{Colors.END}",
                repo_path,
            )
        except InvalidGitRepositoryError:
            # if the path is not a valid git repository, clone it
            await asyncio.to_thread(_clone_origin, repo.repo_url, repo_path, proxy_env)
            logger.info(
                f"Cloned repository from %s to: {Colors.GREEN}%s{Colors.END}",
                repo.repo_url,
                repo_path,
            )
    except GitError:
        logger.error(
            f"Failed to synchronize the repository: {Colors.RED}%s{Colors.END}",
            repo.repo_name,
            exc_info=True,
        )

    # scan the directory for changes
    await scan_directory(repo_path, repo.repo_name)

    # update the last synchronized time
    repo.updated_at = timezone.now()
    await repo.save()


def _pull_origin(repo_path: Path, proxy_env: dict[str, str]):
    """Pull a repository with proxy settings and a bounded Git timeout."""
    repository = Repo(repo_path)
    with repository.git.custom_environment(**proxy_env):
        repository.remotes.origin.pull(kill_after_timeout=_GIT_SYNC_TIMEOUT)


def _clone_origin(repo_url: str, repo_path: Path, proxy_env: dict[str, str]):
    """Clone a repository with proxy settings and a bounded Git timeout.

    This intentionally uses the low-level Git clone command because `Repo.clone_from`
    runs through `as_process=True`, where `kill_after_timeout` does not take effect.
    """
    Git.check_unsafe_protocols(repo_url)
    git = Git()
    with git.custom_environment(**proxy_env):
        git.clone("--", repo_url, str(repo_path), kill_after_timeout=_GIT_SYNC_TIMEOUT)


async def scan_directory(path: Path, repo: str):
    """Scan the directory for flow graph templates and update the database.

    Args:
        path: The path to the repository.
        repo: The name of the repository.
    """
    # update the newest flag for all templates in the repository
    await FlowTemplate.filter(repo_id=repo).update(newest=False)

    # find all JSON files in the repository
    existing_ids = []
    for file in [*path.glob("*.json"), *path.glob("*/*.json")]:
        # read and parse the JSON file
        async with aiofiles.open(file, "rb") as f:
            data = json.try_loads((await f.read()).decode(ENCODING), with_comments=True)

        # validate the template
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        category = data.get("category")
        revision = data.get("revision")
        definition = data.get("definition")
        if not name or not category or not revision or not definition:
            continue

        tmpl_path = str(file.relative_to(path))
        tmpl = await FlowTemplate.get_or_none(
            repo_id=repo, path=tmpl_path, revision=revision
        )
        if not tmpl:
            # create a new template
            await FlowTemplate.create(
                repo_id=repo,
                path=tmpl_path,
                name=name,
                icon=await save_icon(data.get("icon")),
                description=data.get("description"),
                category=category,
                revision=revision,
                definition=definition,
                newest=True,
            )
        else:
            # collect the IDs of existing templates
            if tmpl.icon:
                await save_icon(data.get("icon"), tmpl.icon)
            existing_ids.append(tmpl.id)

    if existing_ids:
        # update the newest flag for existing templates
        await FlowTemplate.filter(id__in=existing_ids).update(newest=True)


async def save_icon(icon: Any, path: str | None = None) -> str | None:
    """Save the icon file to the workspace.

    Args:
        icon: The uploaded icon file or a base64 encoded string.
        path: The relative path to the existing icon file, if any.

    Returns:
        The relative path to the saved icon file, or `None` if no icon is provided.
    """
    if icon is None:
        return None

    icon_bytes = None
    if isinstance(icon, str):
        # decode base64 encoded icon string
        b64str = icon.split(",")[1]
        icon_bytes = base64.b64decode(b64str)
    elif isinstance(icon, File):
        # get the body of the uploaded file
        icon_bytes = icon.body
    elif isinstance(icon, bytes):
        # if icon is already bytes, use it directly
        icon_bytes = icon

    if not icon_bytes:
        logger.error("No icon data provided.")
        return None

    # ensure the images directory exists
    images = Path(KaloscopeConfig.get_workspace("images"))
    (images / "icons").mkdir(parents=True, exist_ok=True)

    # generate a unique filename for the new icon
    if not path:
        path = f"icons/{uuid.uuid4().hex}.webp"

    # save the icon file if it doesn't exist
    icon = images / path
    if not icon.exists():
        async with aiofiles.open(icon, "wb") as f:
            await f.write(icon_bytes)

    return path
