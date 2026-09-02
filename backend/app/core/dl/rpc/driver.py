from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core.dl.driver import (
    DownloadAction,
    DownloadIdentity,
    DownloadRequest,
    DownloadSnapshot,
    DownloadSource,
    DownloadState,
)
from app.core.dl.rpc.client import RpcClient
from app.core.dl.rpc.models import Method, RpcConfig

_METHOD_ACTIONS = {
    "pause": DownloadAction.PAUSE,
    "start": DownloadAction.RESUME,
    "delete": DownloadAction.DELETE,
}


@dataclass(slots=True)
class RpcDriver:
    """A downloader driver backed by a declarative RPC client."""

    config: RpcConfig
    client: RpcClient = field(init=False)

    def __post_init__(self):
        self.client = RpcClient(self.config)

    @property
    def source_types(self) -> frozenset[DownloadSource]:
        source_types = set()
        if "add_link" in self.config.methods:
            source_types.add(DownloadSource.MAGNET)
        if "add_torrent" in self.config.methods:
            source_types.add(DownloadSource.TORRENT)
        return frozenset(source_types)

    @property
    def supports_version(self) -> bool:
        return "version" in self.config.methods

    async def validate(self) -> str | None:
        return await self.version()

    async def version(self) -> str | None:
        return await self.client.version()

    def prepare(self, request: DownloadRequest):
        """Skip pre-submission persistence for an RPC download.

        Args:
            request: The normalized download request.

        Returns:
            Always `None` because RPC tasks are created after submission.
        """
        return None

    async def add(self, request: DownloadRequest) -> DownloadSnapshot:
        result = await self.client.call(
            "add_torrent" if request.torrent else "add_link",
            {
                "dir": request.directory,
                "link": request.link,
                "torrent": request.torrent,
                "pause": request.paused,
                "transfer_lib_id": request.transfer_library_id,
                "transfer_method": request.transfer_method,
                "sub_pattern": request.sub_pattern,
                "sub_repl": request.sub_repl,
            },
        )
        remote_id = result.get("unique_id") if isinstance(result, dict) else None
        return DownloadSnapshot(
            identity=DownloadIdentity(
                remote_id=str(remote_id) if remote_id is not None else None
            )
        )

    async def sync(
        self, identities: Sequence[DownloadIdentity]
    ) -> tuple[DownloadSnapshot, ...]:
        """Defer RPC synchronization to the shared persisted-task synchronizer.

        Args:
            identities: The task identities supplied by the common interface.

        Returns:
            Always an empty tuple because the shared synchronizer owns RPC updates.
        """
        return ()

    async def capabilities(
        self,
        identity: DownloadIdentity,
        state: DownloadState,
        *,
        retry_target: DownloadState | None = None,
    ) -> frozenset[DownloadAction]:
        allowed = {DownloadAction.DELETE}
        if state is DownloadState.DOWNLOADING:
            allowed.add(DownloadAction.PAUSE)
        elif state is DownloadState.PAUSED:
            allowed.add(DownloadAction.RESUME)
        return frozenset(
            action
            for method, action in _METHOD_ACTIONS.items()
            if method in self.config.methods and action in allowed
        )

    async def pause(self, identity: DownloadIdentity) -> DownloadState:
        await self._call("pause", identity)
        return DownloadState.PAUSED

    async def resume(self, identity: DownloadIdentity) -> DownloadState:
        await self._call("start", identity)
        return DownloadState.DOWNLOADING

    async def cancel(self, identity: DownloadIdentity):
        """Ignore cancellation because RPC drivers have no shared cancel method.

        Args:
            identity: The task identity.
        """
        return None

    async def retry(self, identity: DownloadIdentity) -> DownloadState | None:
        """Return no retry transition for an RPC task.

        Args:
            identity: The task identity.

        Returns:
            Always `None` because RPC drivers have no shared retry method.
        """
        return None

    async def delete(self, identity: DownloadIdentity, *, local: bool = False):
        await self._call("delete", identity, local=local)

    async def close(self):
        return None

    async def _call(
        self, method: Method, identity: DownloadIdentity, **variables: object
    ):
        await self.client.call(method, identity.rpc_variables | variables)
