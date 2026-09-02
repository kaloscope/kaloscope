import re
from collections.abc import Iterable

import httpx
from pydantic import BaseModel

from app.core.dl.endpoint import Endpoint
from app.core.dl.openlist.models import (
    OpenListDirectoryAccess,
    OpenListErrorKind,
    OpenListResponse,
    OpenListSettings,
    OpenListSubmitResult,
    OpenListTools,
    RemoteEntryPage,
    RemoteLink,
    RemoteTask,
    RemoteTasks,
)
from app.utils.json import JSONType

_HEADER_PATTERN = re.compile(
    r"\b(authorization|cookie)\s*[:=]\s*(?:bearer\s+)?[^\s,]+", re.I
)
_URL_QUERY_PATTERN = re.compile(r"(https?://[^\s?#]+)[?#]\S+", re.I)
_OFFLINE_TASK_PATH = "/task/offline_download"
_TRANSFER_TASK_PATH = "/task/offline_download_transfer"
_UNSUPPORTED_TOOLS = frozenset({"SimpleHttp", "aria2", "qBittorrent", "Transmission"})


class OpenListClientError(Exception):
    """Represent a classified OpenList request failure."""

    def __init__(
        self,
        kind: OpenListErrorKind,
        message: str = "OpenList request failed",
        retry_after: str | None = None,
        response_message: str | None = None,
    ):
        self.kind = kind
        self.retry_after = retry_after
        self.response_message = response_message
        super().__init__(message)


class OpenListClient:
    """Call OpenList control-plane endpoints through a shared HTTP client.

    Endpoint methods validate the OpenList response envelope and expose failures as
    `OpenListClientError` instances with retry-relevant categories.
    """

    def __init__(self, config: Endpoint, client: httpx.AsyncClient):
        self._base_url = config.base_url
        auth = getattr(config, "auth", None)
        self._token = auth.token.get_secret_value() if auth else ""
        self._remote_root = getattr(config, "remote_root", "")
        self._tool = getattr(config, "tool", "")
        self._client = client

    async def tools(self, path: str) -> tuple[str, ...]:
        """List the OpenList tools available for a destination path.

        Args:
            path: The absolute OpenList destination path.

        Returns:
            The supported offline download tool names.
        """
        data = await self._request(
            "GET", "/public/offline_download_tools", params={"path": path}
        )
        return tuple(
            tool
            for tool in _validate(OpenListTools, data).root
            if tool not in _UNSUPPORTED_TOOLS
        )

    async def version(self) -> str:
        """Get the normalized OpenList version.

        Returns:
            The version without an optional build suffix.
        """
        data = await self._request("GET", "/public/settings")
        version = _validate(OpenListSettings, data).version
        return re.sub(r"\s*\(.*", "", version)

    async def remote_root_writable(self) -> bool:
        """Check whether the configured remote root is writable.

        Returns:
            `True` when OpenList reports write access, otherwise `False`.
        """
        data = await self._request(
            "POST",
            "/fs/list",
            json={
                "path": self._remote_root,
                "page": 1,
                "per_page": 1,
                "refresh": False,
            },
        )
        return _validate(OpenListDirectoryAccess, data).write

    async def require_admin(self):
        """Require administrator access for the configured token."""
        await self._request("GET", "/admin/driver/names")

    async def mkdir(self, path: str):
        """Create a remote directory.

        Args:
            path: The absolute OpenList directory path.
        """
        await self._request("POST", "/fs/mkdir", json={"path": path})

    async def submit(self, source: str, path: str) -> tuple[str, ...]:
        """Submit one source to the configured offline download tool.

        Args:
            source: The URI accepted by the configured tool.
            path: The isolated remote result directory.

        Returns:
            The remote task IDs returned by OpenList.
        """
        data = await self._request(
            "POST",
            "/fs/add_offline_download",
            json={
                "urls": [source],
                "path": path,
                "tool": self._tool,
                "delete_policy": "delete_on_upload_succeed",
            },
            secrets=(source,),
        )
        result = _validate(OpenListSubmitResult, data)
        return tuple(task.id for task in result.tasks)

    async def undone(self) -> tuple[RemoteTask, ...]:
        """List unfinished offline download tasks.

        Returns:
            The unfinished remote tasks.
        """
        return await self._tasks(f"{_OFFLINE_TASK_PATH}/undone")

    async def done(self) -> tuple[RemoteTask, ...]:
        """List finished offline download tasks.

        Returns:
            The finished remote tasks.
        """
        return await self._tasks(f"{_OFFLINE_TASK_PATH}/done")

    async def cancel(self, task_id: str):
        """Cancel an offline download task.

        Args:
            task_id: The remote OpenList task ID.
        """
        await self._request(
            "POST",
            f"{_OFFLINE_TASK_PATH}/cancel",
            params={"tid": task_id},
        )

    async def retry(self, task_id: str):
        """Retry an offline download task.

        Args:
            task_id: The remote OpenList task ID.
        """
        await self._request(
            "POST",
            f"{_OFFLINE_TASK_PATH}/retry",
            params={"tid": task_id},
        )

    async def transfer_undone(self) -> tuple[RemoteTask, ...]:
        """List unfinished offline transfer tasks.

        Returns:
            The unfinished remote transfer tasks.
        """
        return await self._tasks(f"{_TRANSFER_TASK_PATH}/undone")

    async def transfer_done(self) -> tuple[RemoteTask, ...]:
        """List finished offline transfer tasks.

        Returns:
            The finished remote transfer tasks.
        """
        return await self._tasks(f"{_TRANSFER_TASK_PATH}/done")

    async def cancel_transfer(self, task_id: str):
        """Cancel an offline transfer task.

        Args:
            task_id: The remote OpenList transfer task ID.
        """
        await self._request(
            "POST",
            f"{_TRANSFER_TASK_PATH}/cancel",
            params={"tid": task_id},
        )

    async def retry_transfer(self, task_id: str):
        """Retry an offline transfer task.

        Args:
            task_id: The remote OpenList transfer task ID.
        """
        await self._request(
            "POST",
            f"{_TRANSFER_TASK_PATH}/retry",
            params={"tid": task_id},
        )

    async def list(
        self, path: str, page: int, per_page: int, refresh: bool = False
    ) -> RemoteEntryPage:
        """List one page of a remote directory.

        Args:
            path: The absolute OpenList directory path.
            page: The one-based page number.
            per_page: The maximum entries requested per page.
            refresh: Whether OpenList should refresh the mounted storage first.

        Returns:
            The validated directory page.
        """
        data = await self._request(
            "POST",
            "/fs/list",
            json={
                "path": path,
                "page": page,
                "per_page": per_page,
                "refresh": refresh,
            },
        )
        return _validate(RemoteEntryPage, data)

    async def link(self, path: str) -> RemoteLink:
        """Get a direct link for a remote file.

        Args:
            path: The absolute OpenList file path.

        Returns:
            The direct URL and required request headers.
        """
        data = await self._request("POST", "/fs/link", json={"path": path})
        return _validate(RemoteLink, data)

    async def remove(self, directory: str, name: str):
        """Remove one entry from a remote directory.

        Args:
            directory: The absolute parent directory path.
            name: The direct child name to remove.
        """
        await self._request(
            "POST",
            "/fs/remove",
            json={"dir": directory, "names": [name]},
        )

    async def _tasks(self, path: str) -> tuple[RemoteTask, ...]:
        """List and sanitize remote tasks from an OpenList task endpoint.

        Args:
            path: The path relative to the configured API root.

        Returns:
            The validated tasks with safe error text.
        """
        data = await self._request("GET", path)
        tasks = _validate(RemoteTasks, data).root
        for task in tasks:
            task.error = _redact(task.error, (self._token,))
        return tuple(tasks)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: JSONType = None,
        params: dict[str, str] | None = None,
        secrets: Iterable[str] = (),
    ) -> JSONType:
        """Send one request and unwrap its OpenList response envelope.

        Args:
            method: The HTTP method.
            path: The path relative to the configured API root.
            json: The optional JSON request body.
            params: The optional query parameters.
            secrets: The additional values to redact from business errors.

        Raises:
            OpenListClientError: If transport, HTTP, envelope, or business validation
                fails.

        Returns:
            The decoded `data` value from a successful OpenList envelope.
        """
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": self._token} if self._token else None,
                json=json,
                params=params,
                follow_redirects=False,  # do not forward the administrator token
            )
        except httpx.RequestError as exc:
            # keep transport failures retryable
            raise OpenListClientError(OpenListErrorKind.TRANSIENT) from exc
        if response.status_code != 200:
            kind = _error_kind(response.status_code)
            if response.is_redirect:
                kind = OpenListErrorKind.INVALID_RESPONSE
            raise OpenListClientError(
                kind,
                f"OpenList request failed with HTTP {response.status_code}",
                retry_after=response.headers.get("Retry-After"),
            )
        try:
            envelope = OpenListResponse[JSONType].model_validate(response.json())
        except ValueError as exc:
            raise OpenListClientError(
                OpenListErrorKind.INVALID_RESPONSE,
                "OpenList returned an invalid response",
            ) from exc
        if envelope.code != 200:
            # business errors use HTTP `200`
            message = _redact(envelope.message, (self._token, *secrets))
            raise OpenListClientError(
                _error_kind(envelope.code),
                message or "OpenList request failed",
                retry_after=response.headers.get("Retry-After"),
                response_message=message or None,
            )
        return envelope.data


def _redact(message: str, secrets: Iterable[str]) -> str:
    """Redact credentials and sensitive sources from an error message.

    Args:
        message: The untrusted OpenList error message.
        secrets: The additional values that must not be exposed.

    Returns:
        The bounded and sanitized error message.
    """
    # endpoint methods may pass sensitive sources here
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<redacted>")
    message = _HEADER_PATTERN.sub(r"\1: <redacted>", message)
    message = _URL_QUERY_PATTERN.sub(r"\1?<redacted>", message)
    return message[:512]


def _validate[ResponseModel: BaseModel](
    model: type[ResponseModel], data: JSONType
) -> ResponseModel:
    """Validate response data against a strict Pydantic model.

    Args:
        model: The response model used for validation.
        data: The decoded OpenList response data.

    Raises:
        OpenListClientError: If the response data does not match `model`.

    Returns:
        The validated response model.
    """
    try:
        return model.model_validate(data)
    except ValueError as exc:
        raise OpenListClientError(
            OpenListErrorKind.INVALID_RESPONSE,
            "OpenList returned an invalid response",
        ) from exc


def _error_kind(code: int) -> OpenListErrorKind:
    """Classify an OpenList or HTTP response code.

    Args:
        code: The response code to classify.

    Returns:
        The corresponding OpenList failure category.
    """
    if code in {401, 403}:
        return OpenListErrorKind.AUTH
    if code == 429:
        return OpenListErrorKind.RATE_LIMIT
    if code >= 500:
        return OpenListErrorKind.TRANSIENT
    return OpenListErrorKind.API
