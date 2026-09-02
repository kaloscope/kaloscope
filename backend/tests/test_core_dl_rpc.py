import asyncio
from typing import cast

import pytest

from app.core.dl.driver import (
    DownloadAction,
    DownloadIdentity,
    DownloadRequest,
    DownloadSource,
    DownloadState,
)
from app.core.dl.rpc import RpcClient, RpcConfig, RpcDriver
from app.core.dl.rpc.models import API, Method


class RecordingClient:
    def __init__(self, *, result=None):
        self.result = result
        self.calls = []

    async def call(self, method, variables):
        self.calls.append((method, variables))
        return self.result


def _config(*methods: Method) -> RpcConfig:
    return RpcConfig(
        name="test",
        host="example.com",
        port=80,
        methods={method: API() for method in methods},
    )


def test_add_link():
    client = RecordingClient(result={"unique_id": "remote-1"})
    driver = RpcDriver(_config("add_link"))
    driver.client = cast(RpcClient, client)
    request = DownloadRequest(
        directory="/downloads",
        link="magnet:?xt=normalized",
        paused=True,
        transfer_library_id=7,
        transfer_method="copy",
        sub_pattern=r"^old",
        sub_repl="new",
    )

    snapshot = asyncio.run(driver.add(request))

    assert driver.source_types == frozenset({DownloadSource.MAGNET})
    assert snapshot.identity.remote_id == "remote-1"
    assert snapshot.state is None
    assert client.calls == [
        (
            "add_link",
            {
                "dir": "/downloads",
                "link": "magnet:?xt=normalized",
                "torrent": None,
                "pause": True,
                "transfer_lib_id": 7,
                "transfer_method": "copy",
                "sub_pattern": r"^old",
                "sub_repl": "new",
            },
        )
    ]


def test_add_torrent():
    torrent = ("sample.torrent", b"torrent", "application/x-bittorrent")
    client = RecordingClient(result={"unique_id": "remote-torrent"})
    request = DownloadRequest(
        directory="/downloads", link="magnet:?xt=normalized", torrent=torrent
    )

    driver = RpcDriver(_config("add_torrent"))
    driver.client = cast(RpcClient, client)
    snapshot = asyncio.run(driver.add(request))

    method, variables = client.calls[0]
    assert method == "add_torrent"
    assert variables["link"] == "magnet:?xt=normalized"
    assert variables["torrent"] is torrent
    assert snapshot.identity.remote_id == "remote-torrent"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (DownloadState.DOWNLOADING, {DownloadAction.PAUSE, DownloadAction.DELETE}),
        (DownloadState.PAUSED, {DownloadAction.RESUME, DownloadAction.DELETE}),
        (DownloadState.ERROR, {DownloadAction.DELETE}),
    ],
)
def test_capabilities(state, expected):
    driver = RpcDriver(_config("pause", "start", "delete"))
    driver.client = cast(RpcClient, RecordingClient())

    actions = asyncio.run(
        driver.capabilities(DownloadIdentity(remote_id="task-1"), state)
    )

    assert actions == frozenset(expected)


def test_actions():
    client = RecordingClient()
    driver = RpcDriver(_config("pause", "start", "delete"))
    driver.client = cast(RpcClient, client)
    identity = DownloadIdentity(
        remote_id="task-1", info_hash="hash-v1", info_hash_v2="hash-v2"
    )

    paused_state = asyncio.run(driver.pause(identity))
    resumed_state = asyncio.run(driver.resume(identity))
    asyncio.run(driver.delete(identity, local=True))

    assert paused_state is DownloadState.PAUSED
    assert resumed_state is DownloadState.DOWNLOADING
    common = {"id": "task-1", "hash": "hash-v1", "hash_v2": "hash-v2"}
    assert client.calls == [
        ("pause", common),
        ("start", common),
        ("delete", {**common, "local": True}),
    ]
