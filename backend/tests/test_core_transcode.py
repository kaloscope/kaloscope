"""Unit tests for core transcoding."""

import asyncio
import importlib
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from filelock import FileLock

import app.core.transcode.hwaccels.base as base_module
import app.core.transcode.hwaccels.qsv as qsv_module
import app.core.transcode.hwaccels.vaapi as vaapi_module
from app.core.exceptions import KaloscopeException
from app.core.transcode import capabilities as capability_module
from app.core.transcode import hls, tasks, transcoder
from app.core.transcode.capabilities import FFmpegCapabilities
from app.core.transcode.hwaccels import get_hwaccel
from app.core.transcode.hwaccels.base import (
    HDRType,
    MediaProbe,
    TranscodeContext,
    classify_hdr,
)
from app.core.transcode.options import (
    HWAccelType,
    ResolutionLimit,
    TranscodeOptions,
)


@pytest.mark.parametrize(
    ("kind", "output", "expected"),
    [
        (
            "encoders",
            "Encoders:\n V....D libx264 H.264\n A....D aac AAC\n",
            {"libx264", "aac"},
        ),
        (
            "filters",
            "Filters:\n .. scale V->V Scale\n .S tonemap V->V Tone map\n",
            {"scale", "tonemap"},
        ),
        (
            "hwaccels",
            "Hardware acceleration methods:\nvaapi\nqsv\n",
            {"vaapi", "qsv"},
        ),
        (
            "bsfs",
            "Bitstream filters:\nh264_metadata\nnull\n",
            {"h264_metadata", "null"},
        ),
        (
            "muxers",
            "Muxers:\n E  hls Apple HLS\n E  stream_segment,ssegment Segment\n",
            {"hls", "stream_segment", "ssegment"},
        ),
    ],
)
def test_parse_capabilities(kind, output, expected):
    capabilities = importlib.import_module("app.core.transcode.capabilities")

    assert capabilities._parse_listing(kind, output) == expected


def test_parse_encoder_options():
    capabilities = importlib.import_module("app.core.transcode.capabilities")
    output = (
        "h264_videotoolbox AVOptions:\n"
        "  -profile <int> E..V....... Profile\n"
        "  -prio_speed <boolean> E..V....... prioritize speed\n"
    )

    assert capabilities._parse_encoder_options(output) == {"profile", "prio_speed"}


def test_capability_cache(monkeypatch):
    outputs = {
        "-encoders": " V....D libx264 H.264\n A....D aac AAC\n",
        "-filters": " .. scale V->V Scale\n",
        "-hwaccels": "videotoolbox\n",
        "-bsfs": "h264_metadata\n",
        "-muxers": " E  hls Apple HLS\n E  mpegts MPEG-TS\n",
        "-h": "  -preset <string> E..V....... Preset\n",
    }

    async def query(_executable, *args):
        return outputs[args[0]]

    query_mock = AsyncMock(side_effect=query)
    capability_module._clear_caches()
    monkeypatch.setattr(capability_module, "_resolved_executable", lambda value: value)
    monkeypatch.setattr(capability_module, "_query_ffmpeg", query_mock)

    first = asyncio.run(
        capability_module.load_ffmpeg_capabilities("ffmpeg-test", "libx264")
    )
    second = asyncio.run(
        capability_module.load_ffmpeg_capabilities("ffmpeg-test", "libx264")
    )

    assert first is second
    assert first.encoders == {"libx264", "aac"}
    assert first.encoder_options == {"preset"}
    assert query_mock.await_count == 6


class _ProbeStream:
    def __init__(self, *chunks: bytes):
        self.chunks: list[bytes] = list(chunks)

    async def read(self, size: int) -> bytes:
        del size
        return self.chunks.pop(0) if self.chunks else b""


class _ProbeProcess:
    def __init__(
        self,
        returncode: int | None = 0,
        stderr: tuple[bytes, ...] = (),
    ):
        self.returncode: int | None = returncode
        self.stderr = _ProbeStream(*stderr)
        self.kill = Mock()

    async def wait(self) -> int | None:
        return self.returncode


def test_probe_failure_detail(monkeypatch):
    proc = _ProbeProcess(returncode=1, stderr=(b"x" * 3000,))
    monkeypatch.setattr(
        capability_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )

    success, detail = asyncio.run(
        capability_module._run_ffmpeg_probe("ffmpeg-test", [])
    )

    assert success is False
    assert len(detail) == capability_module._RUNTIME_STDERR_LIMIT


def test_probe_start_failure(monkeypatch):
    monkeypatch.setattr(
        capability_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError("unavailable")),
    )

    assert asyncio.run(capability_module._run_ffmpeg_probe("ffmpeg-test", [])) == (
        False,
        "unavailable",
    )


def test_probe_timeout_cleanup(monkeypatch):
    stopped = asyncio.Event()

    class Process(_ProbeProcess):
        def __init__(self):
            super().__init__(returncode=None)
            self.kill = Mock(side_effect=self._stop)

        def _stop(self):
            self.returncode = -9
            stopped.set()

        async def wait(self):
            await stopped.wait()
            return self.returncode

    proc = Process()
    monkeypatch.setattr(
        capability_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    monkeypatch.setattr(capability_module, "_RUNTIME_PROBE_TIMEOUT", 0.01)

    success, detail = asyncio.run(
        capability_module._run_ffmpeg_probe("ffmpeg-test", [])
    )

    assert success is False
    assert detail == "timed out after 0.0 seconds"
    proc.kill.assert_called_once_with()


def test_probe_cancel_cleanup(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Process(_ProbeProcess):
        def __init__(self):
            super().__init__(returncode=None)
            self.kill = Mock(side_effect=self._stop)

        def _stop(self):
            self.returncode = -9
            stopped.set()

        async def wait(self):
            started.set()
            await stopped.wait()
            return self.returncode

        async def communicate(self):
            started.set()
            await stopped.wait()
            return None, b""

    proc = Process()
    monkeypatch.setattr(
        capability_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )

    async def cancel_probe():
        task = asyncio.create_task(
            capability_module._run_ffmpeg_probe("ffmpeg-test", [])
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_probe())

    proc.kill.assert_called_once_with()
    assert proc.returncode == -9


def test_probe_stalled_cleanup(monkeypatch):
    never = asyncio.Event()

    class Process(_ProbeProcess):
        def __init__(self):
            super().__init__(returncode=None)

        async def wait(self):
            await never.wait()

        async def communicate(self):
            await never.wait()

    proc = Process()
    monkeypatch.setattr(
        capability_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    monkeypatch.setattr(capability_module, "_RUNTIME_PROBE_TIMEOUT", 0.01)
    monkeypatch.setattr(capability_module, "_RUNTIME_REAP_TIMEOUT", 0.01, raising=False)

    result = asyncio.run(
        asyncio.wait_for(
            capability_module._run_ffmpeg_probe("ffmpeg-test", []),
            timeout=0.1,
        )
    )

    assert result == (False, "timed out after 0.0 seconds")
    proc.kill.assert_called_once_with()


def test_encoder_probe_cache(monkeypatch):
    probe = AsyncMock(return_value=(True, ""))
    capability_module._clear_caches()
    monkeypatch.setattr(capability_module, "_run_ffmpeg_probe", probe)
    monkeypatch.setattr(
        capability_module,
        "_resolved_executable",
        lambda value: value,
    )

    for _ in range(2):
        asyncio.run(
            capability_module.require_hardware_encoder(
                "ffmpeg-test",
                "vaapi",
                "h264_vaapi",
                "/dev/dri/renderD128",
                [],
            )
        )

    probe.assert_awaited_once()


def test_encoder_probe_failure_cache(monkeypatch):
    probe = AsyncMock(return_value=(False, "device failed"))
    capability_module._clear_caches()
    monkeypatch.setattr(capability_module, "_run_ffmpeg_probe", probe)
    monkeypatch.setattr(
        capability_module,
        "_resolved_executable",
        lambda value: value,
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="h264_vaapi.*device failed"):
            asyncio.run(
                capability_module.require_hardware_encoder(
                    "ffmpeg-test",
                    "vaapi",
                    "h264_vaapi",
                    None,
                    [],
                )
            )

    assert probe.await_count == 2


def test_decode_probe_cache_key(monkeypatch, tmp_path):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"first")
    probe = AsyncMock(return_value=(True, ""))
    capability_module._clear_caches()
    monkeypatch.setattr(capability_module, "_run_ffmpeg_probe", probe)
    monkeypatch.setattr(
        capability_module,
        "_resolved_executable",
        lambda value: value,
    )

    for _ in range(2):
        assert (
            asyncio.run(
                capability_module.probe_hardware_decode(
                    "ffmpeg-test",
                    "vaapi",
                    None,
                    str(media),
                    0,
                    [],
                )
            )
            is True
        )
    media.write_bytes(b"changed-size")
    assert (
        asyncio.run(
            capability_module.probe_hardware_decode(
                "ffmpeg-test",
                "vaapi",
                None,
                str(media),
                0,
                [],
            )
        )
        is True
    )

    assert probe.await_count == 2


def test_decode_probe_failure_cache(monkeypatch, tmp_path):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    probe = AsyncMock(return_value=(False, "decode failed"))
    capability_module._clear_caches()
    monkeypatch.setattr(capability_module, "_run_ffmpeg_probe", probe)
    monkeypatch.setattr(
        capability_module,
        "_resolved_executable",
        lambda value: value,
    )

    for _ in range(2):
        assert (
            asyncio.run(
                capability_module.probe_hardware_decode(
                    "ffmpeg-test",
                    "vaapi",
                    None,
                    str(media),
                    0,
                    [],
                )
            )
            is False
        )

    assert probe.await_count == 2


def test_transform_probe_cache_key(monkeypatch, tmp_path):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"first")
    probe = AsyncMock(return_value=(True, ""))
    capability_module._clear_caches()
    monkeypatch.setattr(capability_module, "_run_ffmpeg_probe", probe)
    monkeypatch.setattr(
        capability_module,
        "_resolved_executable",
        lambda value: value,
    )

    async def run(signature):
        return await capability_module.probe_hardware_transform(
            "ffmpeg-test",
            "vaapi",
            "/dev/dri/renderD128",
            str(media),
            0,
            signature,
            [],
        )

    assert asyncio.run(run("transpose_vaapi=dir=clock")) is True
    assert asyncio.run(run("transpose_vaapi=dir=clock")) is True
    assert asyncio.run(run("transpose_vaapi=dir=cclock")) is True
    media.write_bytes(b"changed-size")
    assert asyncio.run(run("transpose_vaapi=dir=clock")) is True

    assert probe.await_count == 3


def test_transform_probe_failure_cache(monkeypatch, tmp_path):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    probe = AsyncMock(return_value=(False, "filter failed"))
    capability_module._clear_caches()
    monkeypatch.setattr(capability_module, "_run_ffmpeg_probe", probe)
    monkeypatch.setattr(
        capability_module,
        "_resolved_executable",
        lambda value: value,
    )

    for _ in range(2):
        assert (
            asyncio.run(
                capability_module.probe_hardware_transform(
                    "ffmpeg-test",
                    "qsv",
                    "/dev/dri/renderD128",
                    str(media),
                    0,
                    "vpp_qsv=deinterlace=advanced:rate=frame",
                    [],
                )
            )
            is False
        )

    assert probe.await_count == 2


class _Lock:
    def __init__(self):
        self.locked = False

    def acquire(self):
        self.locked = True

    def release(self):
        self.locked = False


def _runtime_task(out_dir, state=tasks.TaskState.RUNNING):
    return {
        "id": "hash:profile",
        "name": "input.mkv",
        "path": "/media/input.mkv",
        "hash": "hash",
        "state": state,
        "duration": 60.0,
        "pid": 123,
        "process_start_id": "original-process",
        "profile": "profile",
        "quality": "medium",
        "resolution": "original",
        "hwaccel": None,
        "out_dir": str(out_dir),
        "started_at": "2026-01-01",
        "finished_at": None,
        "error_msg": None,
    }


def test_register_process_start(monkeypatch, tmp_path):
    store = {}
    process_start_id = AsyncMock(return_value="process-start-id")
    proc = cast(asyncio.subprocess.Process, SimpleNamespace(pid=123))

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, _Lock()))
    monkeypatch.setattr(tasks, "_process_start_id", process_start_id)

    task_id = asyncio.run(
        tasks.register_task(
            "/media/input.mkv",
            "hash",
            TranscodeOptions(),
            tmp_path,
            proc,
            60.0,
        )
    )

    assert store[task_id]["process_start_id"] == "process-start-id"
    process_start_id.assert_awaited_once_with(123)


def test_linux_process_start(monkeypatch):
    fields = ["S", *(str(field) for field in range(4, 23))]
    stat = f"123 (ffmpeg worker) {' '.join(fields)}"

    monkeypatch.setattr(tasks.sys, "platform", "linux")
    monkeypatch.setattr(tasks.Path, "read_text", lambda *_args, **_kwargs: stat)

    assert not hasattr(tasks, "_read_process_start_id")
    assert asyncio.run(tasks._process_start_id(123)) == "linux:22"


def test_macos_process_start(monkeypatch):
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout="Sun Jul 12 12:34:56 2026\n",
        )
    )

    monkeypatch.setattr(tasks.sys, "platform", "darwin")
    monkeypatch.setattr(tasks.subprocess, "run", run)

    assert asyncio.run(tasks._process_start_id(123)) == (
        "darwin:Sun Jul 12 12:34:56 2026"
    )
    run.assert_called_once_with(
        ["ps", "-o", "lstart=", "-p", "123"],
        capture_output=True,
        check=False,
        text=True,
        timeout=2,
    )


def test_scan_exclusions(monkeypatch, tmp_path):
    (tmp_path / "hash" / "profile").mkdir(parents=True)
    output_stats = Mock(side_effect=AssertionError("output scanned"))

    monkeypatch.setattr(hls, "output_stats", output_stats)

    result = hls.scan_outputs(tmp_path, exclude_ids={"hash:profile"})

    assert result == []
    output_stats.assert_not_called()


def test_delete_locked(tmp_path):
    root = tmp_path / "transcoded"
    out_dir = root / "hash" / "profile"
    out_dir.mkdir(parents=True)
    playlist = out_dir / "index.m3u8"
    playlist.write_text("#EXTM3U\n")
    lock = transcoder._acquire_lock(out_dir)
    assert lock is not None

    try:
        assert hls.delete_output("hash", "profile", root=root) is False
    finally:
        transcoder._release_lock(lock)

    assert playlist.is_file()


def test_delete_escape(tmp_path):
    root = tmp_path / "transcoded"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    assert hls.delete_output("..", "outside", root=root) is False
    assert outside.is_dir()


def test_read_fallback(tmp_path):
    playlist = tmp_path / "profile" / "index.m3u8"
    playlist.parent.mkdir()

    assert asyncio.run(hls.read_m3u8(playlist)) == hls._MINIMAL_M3U8
    playlist.write_text("")
    assert asyncio.run(hls.read_m3u8(playlist)) == hls._MINIMAL_M3U8
    playlist.write_text("#EXTM3U\n")
    assert asyncio.run(hls.read_m3u8(playlist)) == "#EXTM3U\n"


def test_wait_segment(tmp_path):
    playlist = tmp_path / "index.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:6.0,\nsegment_000000.ts\n")

    assert asyncio.run(hls.wait_segment(playlist, timeout=0.01, interval=0)) is True


def test_wait_exited(tmp_path):
    proc = SimpleNamespace(returncode=1)

    result = asyncio.run(
        hls.wait_segment(
            tmp_path / "index.m3u8",
            proc=cast(asyncio.subprocess.Process, proc),
            timeout=1,
            interval=0.1,
        )
    )

    assert result is False


def test_probe_media(monkeypatch):
    probe_proc = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(
                b'{"streams":[{"index":0,"codec_type":"video",'
                b'"disposition":{"attached_pic":1},"height":600},'
                b'{"index":1,"codec_type":"audio"},'
                b'{"index":2,"codec_type":"video",'
                b'"codec_name":"hevc","profile":"Main 10",'
                b'"bits_per_raw_sample":"10","bits_per_sample":0,'
                b'"disposition":{"attached_pic":0},'
                b'"avg_frame_rate":"30000/1001",'
                b'"r_frame_rate":"30000/1001",'
                b'"pix_fmt":"yuv420p10le","width":1920,"height":1080,'
                b'"sample_aspect_ratio":"4:3","field_order":"tt",'
                b'"color_range":"tv",'
                b'"color_transfer":"smpte2084","color_primaries":"bt2020",'
                b'"color_space":"bt2020nc",'
                b'"side_data_list":[{"side_data_type":"Display Matrix",'
                b'"rotation":-90},{"side_data_type":"DOVI configuration record",'
                b'"dv_profile":8,"bl_present_flag":1,'
                b'"dv_bl_signal_compatibility_id":1}]}],'
                b'"format":{"duration":"60.5"}}',
                b"",
            )
        ),
    )
    frame_proc = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(
                b'{"frames":[{"side_data_list":['
                b'{"side_data_type":"HDR Dynamic Metadata SMPTE2094-40 '
                b'(HDR10+)"}]}]}',
                b"",
            )
        ),
    )
    create = AsyncMock(side_effect=[probe_proc, frame_proc])

    monkeypatch.setattr(transcoder, "_ffprobe", AsyncMock(return_value="ffprobe"))
    monkeypatch.setattr(transcoder.asyncio, "create_subprocess_exec", create)

    result = asyncio.run(transcoder.probe_media("input.mkv"))

    assert result == MediaProbe(
        video_stream_index=2,
        audio_stream_index=1,
        duration=60.5,
        avg_frame_rate=Fraction(30000, 1001),
        r_frame_rate=Fraction(30000, 1001),
        pixel_format="yuv420p10le",
        width=1920,
        height=1080,
        sample_aspect_ratio=(4, 3),
        rotation=90,
        field_order="tt",
        color_transfer="smpte2084",
        color_primaries="bt2020",
        color_space="bt2020nc",
        codec="hevc",
        profile="Main 10",
        bit_depth=10,
        color_range="tv",
        dovi_profile=8,
        dovi_bl_present=True,
        dovi_bl_signal_compatibility_id=1,
        hdr10_plus=True,
    )
    assert create.await_count == 2
    await_args = create.await_args_list[0]
    assert await_args is not None
    args = await_args.args
    assert (
        "format=duration:chapter=id,start_time,end_time:chapter_tags=title:"
        "stream=index,codec_type,codec_name,profile,"
        "bits_per_sample,bits_per_raw_sample,avg_frame_rate,r_frame_rate,"
        "pix_fmt,width,height,"
        "sample_aspect_ratio,field_order,"
        "color_range,color_transfer,color_primaries,color_space:"
        "stream_disposition=attached_pic:stream_side_data=side_data_type,"
        "rotation,dv_profile,bl_present_flag,dv_bl_signal_compatibility_id"
    ) in args
    assert "-select_streams" not in args
    assert "json" in args

    frame_args = create.await_args_list[1].args
    assert frame_args[frame_args.index("-select_streams") + 1] == "2"
    assert frame_args[frame_args.index("-read_intervals") + 1] == "%+#1"
    assert "-show_frames" in frame_args
    assert (
        frame_args[frame_args.index("-show_entries") + 1]
        == "frame=stream_index:frame_side_data=side_data_type"
    )


def test_probe_media_chapters(monkeypatch):
    proc = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(
                b'{"chapters":['
                b'{"id":9012216534194587836,"start_time":"202.994000",'
                b'"end_time":"587.045000","tags":{"title":"Episode"}},'
                b'{"id":1734728195631322526,"start_time":"0.000000",'
                b'"end_time":"113.030000","tags":{"title":"Opening"}}],'
                b'"format":{"duration":"587.045000"}}',
                b"",
            )
        ),
    )
    monkeypatch.setattr(transcoder, "_ffprobe", AsyncMock(return_value="ffprobe"))
    monkeypatch.setattr(
        transcoder.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )

    result = asyncio.run(transcoder.probe_media("input.mkv"))

    assert [
        {
            "id": chapter.id,
            "title": chapter.title,
            "start": chapter.start,
            "end": chapter.end,
        }
        for chapter in result.chapters
    ] == [
        {
            "id": "1734728195631322526",
            "title": "Opening",
            "start": 0.0,
            "end": 113.03,
        },
        {
            "id": "9012216534194587836",
            "title": "Episode",
            "start": 202.994,
            "end": 587.045,
        },
    ]


@pytest.mark.parametrize(
    ("pixel_format", "expected"),
    [
        ("yuv420p", 8),
        ("nv12", 8),
        ("yuv420p10le", 10),
        ("p010le", 10),
        ("p012le", 12),
        ("unknown", None),
        (None, None),
    ],
)
def test_pixel_format_bit_depth(pixel_format, expected):
    assert transcoder._pixel_format_bit_depth(pixel_format) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1:1", (1, 1)),
        ("16/15", (16, 15)),
        (" 4:3 ", (4, 3)),
        ("0:1", None),
        ("1:0", None),
        ("N/A", None),
        (None, None),
    ],
)
def test_parse_sample_aspect_ratio(value, expected):
    assert transcoder._parse_sample_aspect_ratio(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30000/1001", Fraction(30000, 1001)),
        ("50/2", Fraction(25, 1)),
        ("25", Fraction(25, 1)),
        ("0/0", None),
        ("0/1", None),
        ("-24/1", None),
        ("bad", None),
        (None, None),
        (24, None),
    ],
)
def test_parse_frame_rate(value, expected):
    assert transcoder._parse_frame_rate(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (-90, 90),
        (180, 180),
        (90, 270),
        (-359.95, 0),
        (-45, 45),
        ("bad", None),
        (None, None),
    ],
)
def test_parse_rotation(value, expected):
    assert transcoder._parse_rotation(value) == expected


@pytest.mark.parametrize(
    ("raw_fields", "expected"),
    [
        ('"bits_per_raw_sample":"12","bits_per_sample":10', 12),
        ('"bits_per_raw_sample":"0","bits_per_sample":10', 10),
        ('"bits_per_raw_sample":"N/A","pix_fmt":"p010le"', 10),
    ],
)
def test_probe_bit_depth_precedence(monkeypatch, raw_fields, expected):
    stdout = (
        '{"streams":[{"index":0,"codec_type":"video",'
        f"{raw_fields}" + '}],"format":{"duration":"1"}}'
    ).encode()
    proc = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(stdout, b"")),
    )
    monkeypatch.setattr(transcoder, "_ffprobe", AsyncMock(return_value="ffprobe"))
    monkeypatch.setattr(
        transcoder.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )

    assert asyncio.run(transcoder.probe_media("input.mkv")).bit_depth == expected


def test_probe_hdr10_plus(monkeypatch):
    proc = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(
                b'{"frames":[{"side_data_list":['
                b'{"side_data_type":"HDR Dynamic Metadata SMPTE2094-40 '
                b'(HDR10+)"}]}]}',
                b"",
            )
        ),
    )
    create = AsyncMock(return_value=proc)
    monkeypatch.setattr(transcoder, "_ffprobe", AsyncMock(return_value="ffprobe"))
    monkeypatch.setattr(transcoder.asyncio, "create_subprocess_exec", create)

    assert asyncio.run(transcoder._probe_hdr10_plus("input.mkv", 2)) is True


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, b""),
        (0, b'{"frames":[{"side_data_list":[]}]}'),
        (0, b"invalid"),
    ],
)
def test_hdr10_plus_missing_metadata(monkeypatch, returncode, stdout):
    proc = SimpleNamespace(
        returncode=returncode,
        communicate=AsyncMock(return_value=(stdout, b"")),
    )
    monkeypatch.setattr(transcoder, "_ffprobe", AsyncMock(return_value="ffprobe"))
    monkeypatch.setattr(
        transcoder.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )

    assert asyncio.run(transcoder._probe_hdr10_plus("input.mkv", 2)) is False


def test_hdr10_plus_timeout_cleanup(monkeypatch):
    proc = SimpleNamespace(
        returncode=None,
        kill=Mock(),
        communicate=AsyncMock(side_effect=[TimeoutError, (b"", b"")]),
    )
    monkeypatch.setattr(transcoder, "_ffprobe", AsyncMock(return_value="ffprobe"))
    monkeypatch.setattr(
        transcoder.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )

    assert asyncio.run(transcoder._probe_hdr10_plus("input.mkv", 2)) is False
    proc.kill.assert_called_once_with()
    assert proc.communicate.await_count == 2


def test_hdr10_plus_start_failure(monkeypatch):
    monkeypatch.setattr(transcoder, "_ffprobe", AsyncMock(return_value="ffprobe"))
    monkeypatch.setattr(
        transcoder.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError("unavailable")),
    )

    assert asyncio.run(transcoder._probe_hdr10_plus("input.mkv", 2)) is False


def test_hdr10_plus_non_pq(monkeypatch):
    proc = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(
                b'{"streams":[{"index":0,"codec_type":"video",'
                b'"bits_per_raw_sample":"8","color_transfer":"bt709"}]}',
                b"",
            )
        ),
    )
    hdr10_plus_probe = AsyncMock(return_value=True)
    monkeypatch.setattr(transcoder, "_ffprobe", AsyncMock(return_value="ffprobe"))
    monkeypatch.setattr(
        transcoder.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    monkeypatch.setattr(transcoder, "_probe_hdr10_plus", hdr10_plus_probe)

    result = asyncio.run(transcoder.probe_media("input.mkv"))

    assert result.hdr10_plus is False
    hdr10_plus_probe.assert_not_awaited()


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (1, b"", MediaProbe()),
        (
            0,
            b'{"streams":[{"index":0,"codec_type":"video",'
            b'"avg_frame_rate":"bad"}],"format":{"duration":"60"}}',
            MediaProbe(video_stream_index=0, duration=60.0),
        ),
        (
            0,
            b'{"streams":[{"index":0,"codec_type":"video",'
            b'"avg_frame_rate":"24/1"}],"format":{"duration":"bad"}}',
            MediaProbe(video_stream_index=0, avg_frame_rate=Fraction(24, 1)),
        ),
    ],
)
def test_probe_media_invalid(monkeypatch, returncode, stdout, expected):
    proc = SimpleNamespace(
        returncode=returncode,
        communicate=AsyncMock(return_value=(stdout, b"")),
    )

    monkeypatch.setattr(transcoder, "_ffprobe", AsyncMock(return_value="ffprobe"))
    monkeypatch.setattr(
        transcoder.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )

    assert asyncio.run(transcoder.probe_media("input.mkv")) == expected


def test_probe_timeout(monkeypatch):
    proc = SimpleNamespace(
        returncode=None,
        kill=Mock(),
        communicate=AsyncMock(side_effect=[TimeoutError, (b"", b"")]),
    )

    monkeypatch.setattr(transcoder, "_ffprobe", AsyncMock(return_value="ffprobe"))
    monkeypatch.setattr(
        transcoder.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )

    assert asyncio.run(transcoder.probe_media("input.mkv")) == MediaProbe()
    proc.kill.assert_called_once_with()
    assert proc.communicate.await_count == 2


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            MediaProbe(
                codec="h264",
                profile="High",
                bit_depth=8,
                pixel_format="yuv420p",
            ),
            True,
        ),
        (
            MediaProbe(
                codec="avc",
                profile="Constrained Baseline",
                bit_depth=8,
                pixel_format="nv12",
            ),
            True,
        ),
        (
            MediaProbe(
                codec="hevc",
                profile="Main",
                bit_depth=8,
                pixel_format="yuv420p",
            ),
            True,
        ),
        (
            MediaProbe(
                codec="hevc",
                profile="Main 10",
                bit_depth=10,
                pixel_format="yuv420p10le",
            ),
            True,
        ),
        (
            MediaProbe(
                codec="h264",
                profile="High 10",
                bit_depth=10,
                pixel_format="yuv420p10le",
            ),
            False,
        ),
        (
            MediaProbe(
                codec="hevc",
                profile="Main 10",
                bit_depth=12,
                pixel_format="yuv420p12le",
            ),
            False,
        ),
        (
            MediaProbe(
                codec="hevc",
                profile="Main 10",
                bit_depth=10,
                pixel_format="yuv422p10le",
            ),
            False,
        ),
        (
            MediaProbe(
                codec="hevc",
                profile="Main 10",
                bit_depth=10,
                pixel_format="yuv444p10le",
            ),
            False,
        ),
        (
            MediaProbe(
                codec="vp9",
                profile="Profile 0",
                bit_depth=8,
                pixel_format="yuv420p",
            ),
            False,
        ),
        (
            MediaProbe(
                codec="h264",
                bit_depth=8,
                pixel_format="yuv420p",
            ),
            False,
        ),
        (
            MediaProbe(
                codec="h264",
                profile="High!",
                bit_depth=8,
                pixel_format="yuv420p",
            ),
            False,
        ),
        (
            MediaProbe(
                codec="hevc",
                profile="Main.10",
                bit_depth=10,
                pixel_format="p010le",
            ),
            False,
        ),
    ],
)
def test_decode_candidate(metadata, expected):
    assert base_module._is_decode_candidate(metadata) is expected


def _eligible_hardware_context(
    hwaccel: HWAccelType,
    *,
    capabilities: FFmpegCapabilities | None = None,
    resolution: ResolutionLimit = "original",
) -> TranscodeContext:
    return TranscodeContext(
        options=TranscodeOptions(hwaccel=hwaccel, resolution=resolution),
        metadata=MediaProbe(
            video_stream_index=2,
            codec="h264",
            profile="High",
            bit_depth=8,
            pixel_format="yuv420p",
            height=1080,
        ),
        capabilities=capabilities or _capabilities(),
    )


def test_10_bit_decode_probe(monkeypatch, tmp_path):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    monkeypatch.setattr(
        base_module,
        "require_hardware_encoder",
        AsyncMock(),
        raising=False,
    )
    probe_decode = AsyncMock(return_value=True)
    monkeypatch.setattr(
        base_module,
        "probe_hardware_decode",
        probe_decode,
        raising=False,
    )
    context = _eligible_hardware_context("videotoolbox")
    context.metadata = replace(
        context.metadata,
        bit_depth=10,
        codec="hevc",
        profile="Main 10",
        pixel_format="yuv420p10le",
    )
    context.media_path = str(media)

    asyncio.run(get_hwaccel("videotoolbox").prepare_hardware(context))

    decode_call = probe_decode.await_args
    assert decode_call is not None
    decode_args = decode_call.args[-1]
    assert decode_args[decode_args.index("-vf") + 1] == "hwdownload,format=p010le"


def test_nvenc_decode_fallback(monkeypatch, tmp_path):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    require_encoder = AsyncMock()
    probe_decode = AsyncMock(return_value=False)
    monkeypatch.setattr(
        base_module,
        "require_hardware_encoder",
        require_encoder,
        raising=False,
    )
    monkeypatch.setattr(
        base_module,
        "probe_hardware_decode",
        probe_decode,
        raising=False,
    )
    context = _eligible_hardware_context("nvenc")
    context.media_path = str(media)

    runtime = asyncio.run(get_hwaccel("nvenc").prepare_hardware(context))

    assert runtime == base_module.HardwareRuntime("0", False)
    require_encoder.assert_awaited_once()
    encoder_call = require_encoder.await_args
    decode_call = probe_decode.await_args
    assert encoder_call is not None
    assert decode_call is not None
    assert "h264_nvenc" in encoder_call.args[-1]
    assert "cuda" in decode_call.args[-1]


@pytest.mark.parametrize(
    (
        "hwaccel",
        "device",
        "input_hwaccel",
        "output_format",
        "probe_download",
        "encoder",
        "expected_filter",
    ),
    [
        (
            "nvenc",
            "0",
            "cuda",
            "cuda",
            "yuv420p",
            "h264_nvenc",
            "scale_cuda=w=1280:h=720:format=yuv420p,setsar=1",
        ),
        (
            "vaapi",
            "/dev/dri/renderD128",
            "vaapi",
            "vaapi",
            "nv12",
            "h264_vaapi",
            "scale_vaapi=w=1280:h=720:format=nv12,setsar=1",
        ),
        (
            "qsv",
            "/dev/dri/renderD128",
            "qsv",
            "qsv",
            "nv12",
            "h264_qsv",
            "vpp_qsv=w=1280:h=720:format=nv12,setsar=1",
        ),
        (
            "videotoolbox",
            None,
            "videotoolbox",
            "videotoolbox_vld",
            "nv12",
            "h264_videotoolbox",
            "scale_vt=w=1280:h=720,setsar=1",
        ),
    ],
)
def test_scaled_hardware_command(
    monkeypatch,
    tmp_path,
    hwaccel,
    device,
    input_hwaccel,
    output_format,
    probe_download,
    encoder,
    expected_filter,
):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    strategy = get_hwaccel(hwaccel)
    monkeypatch.setattr(base_module, "require_hardware_encoder", AsyncMock())
    monkeypatch.setattr(
        base_module,
        "probe_hardware_decode",
        AsyncMock(return_value=True),
    )
    probe_transform = AsyncMock(return_value=True)
    monkeypatch.setattr(base_module, "probe_hardware_transform", probe_transform)
    monkeypatch.setattr(
        strategy,
        "resolve_device",
        AsyncMock(return_value=device),
    )
    context = _eligible_hardware_context(hwaccel, resolution="720p")
    context.metadata = replace(
        context.metadata,
        width=1920,
        height=1080,
        field_order="progressive",
    )
    context.media_path = str(media)

    context.hardware = asyncio.run(strategy.prepare_hardware(context))
    cmd = asyncio.run(transcoder._build_hls_cmd(str(media), tmp_path, context))

    assert context.hardware == base_module.HardwareRuntime(device, True, True)
    transform_call = probe_transform.await_args
    assert transform_call is not None
    transform_args = transform_call.args[-1]
    assert transform_args[transform_args.index("-hwaccel_output_format") + 1] == (
        output_format
    )
    assert transform_args[transform_args.index("-vf") + 1] == (
        f"{expected_filter},hwdownload,format={probe_download}"
    )
    assert cmd[cmd.index("-hwaccel") + 1] == input_hwaccel
    assert cmd[cmd.index("-hwaccel_output_format") + 1] == output_format
    assert cmd[cmd.index("-c:v") + 1] == encoder
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == expected_filter
    assert "scale=" not in vf
    assert "hwdownload" not in vf
    assert "hwupload" not in vf


@pytest.mark.parametrize(
    ("hwaccel", "device", "expected_download"),
    [
        ("nvenc", "0", "yuv420p"),
        ("vaapi", "/dev/dri/renderD128", "nv12"),
        ("qsv", "/dev/dri/renderD128", "nv12"),
        ("videotoolbox", None, "p010le"),
    ],
)
def test_10_bit_scale_probe(
    monkeypatch,
    tmp_path,
    hwaccel,
    device,
    expected_download,
):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    strategy = get_hwaccel(hwaccel)
    monkeypatch.setattr(base_module, "require_hardware_encoder", AsyncMock())
    monkeypatch.setattr(
        base_module,
        "probe_hardware_decode",
        AsyncMock(return_value=True),
    )
    probe_transform = AsyncMock(return_value=True)
    monkeypatch.setattr(base_module, "probe_hardware_transform", probe_transform)
    monkeypatch.setattr(
        strategy,
        "resolve_device",
        AsyncMock(return_value=device),
    )
    context = _eligible_hardware_context(hwaccel, resolution="720p")
    context.metadata = replace(
        context.metadata,
        codec="hevc",
        profile="Main 10",
        bit_depth=10,
        pixel_format="p010le",
        width=1920,
        height=1080,
    )
    context.media_path = str(media)

    asyncio.run(strategy.prepare_hardware(context))

    transform_call = probe_transform.await_args
    assert transform_call is not None
    transform_args = transform_call.args[-1]
    assert transform_args[transform_args.index("-vf") + 1].endswith(
        f"hwdownload,format={expected_download}"
    )


@pytest.mark.parametrize("fallback", ["missing_filter", "runtime_failure"])
@pytest.mark.parametrize(
    (
        "hwaccel",
        "device",
        "scaler",
        "expected_hwaccel",
        "encoder",
        "expected_filter",
        "expected_decode",
    ),
    [
        (
            "nvenc",
            "0",
            "scale_cuda",
            "cuda",
            "h264_nvenc",
            "scale=1280:720,setsar=1,format=yuv420p",
            True,
        ),
        (
            "vaapi",
            "/dev/dri/renderD128",
            "scale_vaapi",
            "vaapi",
            "h264_vaapi",
            "scale=1280:720,setsar=1,format=nv12,hwupload",
            True,
        ),
        (
            "qsv",
            "/dev/dri/renderD128",
            "vpp_qsv",
            None,
            "h264_qsv",
            "scale=1280:720,setsar=1,format=nv12",
            False,
        ),
        (
            "videotoolbox",
            None,
            "scale_vt",
            "videotoolbox",
            "h264_videotoolbox",
            "scale=1280:720,setsar=1,format=nv12",
            True,
        ),
    ],
)
def test_scaled_cpu_fallback(
    monkeypatch,
    tmp_path,
    fallback,
    hwaccel,
    device,
    scaler,
    expected_hwaccel,
    encoder,
    expected_filter,
    expected_decode,
):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    strategy = get_hwaccel(hwaccel)
    monkeypatch.setattr(base_module, "require_hardware_encoder", AsyncMock())
    monkeypatch.setattr(
        base_module,
        "probe_hardware_decode",
        AsyncMock(return_value=True),
    )
    probe_transform = AsyncMock(return_value=False)
    monkeypatch.setattr(base_module, "probe_hardware_transform", probe_transform)
    monkeypatch.setattr(
        strategy,
        "resolve_device",
        AsyncMock(return_value=device),
    )
    capabilities = _capabilities()
    if fallback == "missing_filter":
        capabilities = _capabilities(filters=capabilities.filters - {scaler})
    context = _eligible_hardware_context(
        hwaccel,
        capabilities=capabilities,
        resolution="720p",
    )
    context.metadata = replace(context.metadata, width=1920, height=1080)
    context.media_path = str(media)

    context.hardware = asyncio.run(strategy.prepare_hardware(context))
    cmd = asyncio.run(transcoder._build_hls_cmd(str(media), tmp_path, context))

    assert context.hardware == base_module.HardwareRuntime(
        device,
        expected_decode,
        False,
    )
    assert "-hwaccel_output_format" not in cmd
    if expected_hwaccel is None:
        assert "-hwaccel" not in cmd
    else:
        assert cmd[cmd.index("-hwaccel") + 1] == expected_hwaccel
    assert cmd[cmd.index("-c:v") + 1] == encoder
    assert cmd[cmd.index("-vf") + 1] == expected_filter
    if fallback == "missing_filter":
        probe_transform.assert_not_awaited()
    else:
        probe_transform.assert_awaited_once()


@pytest.mark.parametrize(
    ("hwaccel", "device", "encoder", "expected_filter"),
    [
        (
            "nvenc",
            "0",
            "h264_nvenc",
            "scale=1280:720,setsar=1,format=yuv420p",
        ),
        (
            "vaapi",
            "/dev/dri/renderD128",
            "h264_vaapi",
            "scale=1280:720,setsar=1,format=nv12,hwupload",
        ),
        (
            "qsv",
            "/dev/dri/renderD128",
            "h264_qsv",
            "scale=1280:720,setsar=1,format=nv12",
        ),
        (
            "videotoolbox",
            None,
            "h264_videotoolbox",
            "scale=1280:720,setsar=1,format=nv12",
        ),
    ],
)
def test_scaled_unsupported_source(
    monkeypatch,
    tmp_path,
    hwaccel,
    device,
    encoder,
    expected_filter,
):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    strategy = get_hwaccel(hwaccel)
    monkeypatch.setattr(base_module, "require_hardware_encoder", AsyncMock())
    probe_decode = AsyncMock(return_value=True)
    probe_transform = AsyncMock(return_value=True)
    monkeypatch.setattr(base_module, "probe_hardware_decode", probe_decode)
    monkeypatch.setattr(base_module, "probe_hardware_transform", probe_transform)
    monkeypatch.setattr(
        strategy,
        "resolve_device",
        AsyncMock(return_value=device),
    )
    context = _eligible_hardware_context(hwaccel, resolution="720p")
    context.metadata = replace(
        context.metadata,
        codec="vp9",
        profile="Profile 0",
        bit_depth=8,
        pixel_format="yuv420p",
        width=1920,
        height=1080,
    )
    context.media_path = str(media)

    context.hardware = asyncio.run(strategy.prepare_hardware(context))
    cmd = asyncio.run(transcoder._build_hls_cmd(str(media), tmp_path, context))

    assert context.hardware == base_module.HardwareRuntime(device, False, False)
    probe_decode.assert_not_awaited()
    probe_transform.assert_not_awaited()
    assert cmd[cmd.index("-c:v") + 1] == encoder
    assert cmd[cmd.index("-vf") + 1] == expected_filter


@pytest.mark.parametrize("fallback", ["missing_filter", "runtime_failure"])
@pytest.mark.parametrize(
    (
        "hwaccel",
        "device",
        "encoder",
        "field_order",
        "rotation",
        "hardware_filter",
        "expected_hwaccel",
        "expected_filter",
        "expected_decode",
    ),
    [
        (
            "nvenc",
            "0",
            "h264_nvenc",
            "tt",
            0,
            "yadif_cuda",
            "cuda",
            ("bwdif=mode=send_frame:parity=tff:deint=all,setfield=prog,format=yuv420p"),
            True,
        ),
        (
            "vaapi",
            "/dev/dri/renderD128",
            "h264_vaapi",
            "tt",
            90,
            "deinterlace_vaapi",
            "vaapi",
            (
                "bwdif=mode=send_frame:parity=tff:deint=all,"
                "setfield=prog,transpose=clock,format=nv12,hwupload"
            ),
            True,
        ),
        (
            "qsv",
            "/dev/dri/renderD128",
            "h264_qsv",
            "tt",
            90,
            "vpp_qsv",
            None,
            (
                "bwdif=mode=send_frame:parity=tff:deint=all,"
                "setfield=prog,transpose=clock,format=nv12"
            ),
            False,
        ),
        (
            "videotoolbox",
            None,
            "h264_videotoolbox",
            "progressive",
            90,
            "transpose_vt",
            "videotoolbox",
            "transpose=clock,format=nv12",
            True,
        ),
    ],
)
def test_transform_cpu_fallback(
    monkeypatch,
    tmp_path,
    fallback,
    hwaccel,
    device,
    encoder,
    field_order,
    rotation,
    hardware_filter,
    expected_hwaccel,
    expected_filter,
    expected_decode,
):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    strategy = get_hwaccel(hwaccel)
    monkeypatch.setattr(base_module, "require_hardware_encoder", AsyncMock())
    monkeypatch.setattr(
        base_module,
        "probe_hardware_decode",
        AsyncMock(return_value=True),
    )
    probe_transform = AsyncMock(return_value=False)
    monkeypatch.setattr(base_module, "probe_hardware_transform", probe_transform)
    monkeypatch.setattr(
        strategy,
        "resolve_device",
        AsyncMock(return_value=device),
    )
    capabilities = _capabilities()
    if fallback == "missing_filter":
        capabilities = _capabilities(
            filters=capabilities.filters - {hardware_filter},
        )
    context = _eligible_hardware_context(hwaccel, capabilities=capabilities)
    context.metadata = replace(
        context.metadata,
        width=1920,
        rotation=rotation,
        field_order=field_order,
    )
    context.media_path = str(media)

    context.hardware = asyncio.run(strategy.prepare_hardware(context))
    cmd = asyncio.run(transcoder._build_hls_cmd(str(media), tmp_path, context))

    assert context.hardware == base_module.HardwareRuntime(
        device,
        expected_decode,
        False,
    )
    assert cmd[cmd.index("-c:v") + 1] == encoder
    assert cmd[cmd.index("-vf") + 1] == expected_filter
    assert "-hwaccel_output_format" not in cmd
    if expected_hwaccel is None:
        assert "-hwaccel" not in cmd
    else:
        assert cmd[cmd.index("-hwaccel") + 1] == expected_hwaccel
    if fallback == "missing_filter":
        probe_transform.assert_not_awaited()
    else:
        probe_transform.assert_awaited_once()


@pytest.mark.parametrize(
    ("hwaccel", "device", "encoder", "expected_filter"),
    [
        ("nvenc", "0", "h264_nvenc", "format=yuv420p"),
        ("qsv", "/dev/dri/renderD128", "h264_qsv", "format=nv12"),
        (
            "vaapi",
            "/dev/dri/renderD128",
            "h264_vaapi",
            "format=nv12,hwupload",
        ),
        ("videotoolbox", None, "h264_videotoolbox", "format=nv12"),
    ],
)
def test_decode_runtime_fallback(
    monkeypatch,
    tmp_path,
    hwaccel,
    device,
    encoder,
    expected_filter,
):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    strategy = get_hwaccel(hwaccel)
    require_encoder = AsyncMock()
    monkeypatch.setattr(
        base_module,
        "require_hardware_encoder",
        require_encoder,
        raising=False,
    )
    monkeypatch.setattr(
        base_module,
        "probe_hardware_decode",
        AsyncMock(return_value=False),
        raising=False,
    )
    monkeypatch.setattr(
        strategy,
        "resolve_device",
        AsyncMock(return_value=device),
    )
    context = _eligible_hardware_context(hwaccel)
    context.media_path = str(media)

    context.hardware = asyncio.run(strategy.prepare_hardware(context))
    cmd = asyncio.run(transcoder._build_hls_cmd(str(media), tmp_path, context))

    assert context.hardware == base_module.HardwareRuntime(device, False)
    assert "-hwaccel" not in cmd
    assert cmd[cmd.index("-c:v") + 1] == encoder
    assert cmd[cmd.index("-vf") + 1] == expected_filter
    require_encoder.assert_awaited_once()


@pytest.mark.parametrize(
    "case",
    ["unsupported_codec", "missing_hwaccel", "qsv_hdr"],
)
def test_ineligible_decode(monkeypatch, tmp_path, case):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    require_encoder = AsyncMock()
    probe_decode = AsyncMock(return_value=True)
    monkeypatch.setattr(
        base_module,
        "require_hardware_encoder",
        require_encoder,
        raising=False,
    )
    monkeypatch.setattr(
        base_module,
        "probe_hardware_decode",
        probe_decode,
        raising=False,
    )
    monkeypatch.setattr(
        qsv_module,
        "resolve_vaapi_device",
        AsyncMock(return_value="/dev/dri/renderD128"),
    )
    if case == "unsupported_codec":
        context = TranscodeContext(
            options=TranscodeOptions(hwaccel="nvenc"),
            metadata=MediaProbe(
                video_stream_index=0,
                codec="vp9",
                profile="Profile 0",
                bit_depth=8,
                pixel_format="yuv420p",
            ),
            capabilities=_capabilities(),
        )
    elif case == "missing_hwaccel":
        context = _eligible_hardware_context(
            "nvenc",
            capabilities=_capabilities(hwaccels=()),
        )
    else:
        context = TranscodeContext(
            options=TranscodeOptions(hwaccel="qsv"),
            metadata=MediaProbe(
                video_stream_index=0,
                codec="hevc",
                profile="Main 10",
                bit_depth=10,
                pixel_format="yuv420p10le",
                color_transfer="smpte2084",
                color_primaries="bt2020",
                color_space="bt2020nc",
            ),
            capabilities=_capabilities(),
        )

    runtime = asyncio.run(
        get_hwaccel(context.options.hwaccel).prepare_hardware(context)
    )

    assert runtime is not None
    assert runtime.can_decode is False
    require_encoder.assert_awaited_once()
    probe_decode.assert_not_awaited()


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (MediaProbe(bit_depth=8, color_transfer="bt709"), HDRType.SDR),
        (
            MediaProbe(
                bit_depth=10,
                color_transfer="SMPTE2084",
                color_primaries="BT2020",
                color_space="BT2020NC",
            ),
            HDRType.HDR10,
        ),
        (
            MediaProbe(
                bit_depth=10,
                color_transfer="arib-std-b67",
                color_primaries="bt2020",
                color_space="bt2020_ncl",
            ),
            HDRType.HLG,
        ),
        (
            MediaProbe(
                bit_depth=10,
                color_transfer="smpte2084",
                color_primaries="bt2020",
                color_space="bt2020nc",
                hdr10_plus=True,
            ),
            HDRType.HDR10_PLUS,
        ),
        (
            MediaProbe(
                dovi_profile=8,
                dovi_bl_present=True,
                dovi_bl_signal_compatibility_id=1,
            ),
            HDRType.DOVI_COMPATIBLE,
        ),
        (
            MediaProbe(
                bit_depth=10,
                color_transfer="smpte2084",
                color_primaries="bt2020",
                color_space="bt2020nc",
                dovi_profile=7,
                dovi_bl_present=True,
                dovi_bl_signal_compatibility_id=6,
            ),
            HDRType.DOVI_COMPATIBLE,
        ),
        (
            MediaProbe(
                bit_depth=10,
                color_transfer="smpte2084",
                color_primaries="bt2020",
                color_space="bt2020nc",
                dovi_profile=8,
                dovi_bl_present=True,
                dovi_bl_signal_compatibility_id=6,
            ),
            HDRType.DOVI_COMPATIBLE,
        ),
        (
            MediaProbe(
                bit_depth=8,
                color_transfer="smpte2084",
                color_primaries="bt2020",
                color_space="bt2020nc",
                dovi_profile=7,
                dovi_bl_present=True,
                dovi_bl_signal_compatibility_id=6,
            ),
            HDRType.DOVI_ONLY,
        ),
        (
            MediaProbe(
                bit_depth=10,
                color_transfer="bt709",
                color_primaries="bt2020",
                color_space="bt2020nc",
                dovi_profile=7,
                dovi_bl_present=True,
                dovi_bl_signal_compatibility_id=6,
            ),
            HDRType.DOVI_ONLY,
        ),
        (
            MediaProbe(
                bit_depth=10,
                color_transfer="smpte2084",
                color_primaries="bt709",
                color_space="bt2020nc",
                dovi_profile=7,
                dovi_bl_present=True,
                dovi_bl_signal_compatibility_id=6,
            ),
            HDRType.DOVI_ONLY,
        ),
        (
            MediaProbe(
                bit_depth=10,
                color_transfer="smpte2084",
                color_primaries="bt2020",
                color_space="bt709",
                dovi_profile=7,
                dovi_bl_present=True,
                dovi_bl_signal_compatibility_id=6,
            ),
            HDRType.DOVI_ONLY,
        ),
        (
            MediaProbe(
                bit_depth=10,
                color_transfer="smpte2084",
                color_primaries="bt2020",
                color_space="bt2020nc",
                dovi_profile=7,
                dovi_bl_present=False,
                dovi_bl_signal_compatibility_id=6,
            ),
            HDRType.DOVI_ONLY,
        ),
        (
            MediaProbe(
                bit_depth=10,
                dovi_profile=4,
                dovi_bl_present=True,
                dovi_bl_signal_compatibility_id=2,
            ),
            HDRType.DOVI_ONLY,
        ),
        (
            MediaProbe(
                dovi_profile=5,
                dovi_bl_present=True,
                dovi_bl_signal_compatibility_id=0,
            ),
            HDRType.DOVI_ONLY,
        ),
        (
            MediaProbe(
                bit_depth=8,
                color_transfer="smpte2084",
                color_primaries="bt2020",
                color_space="bt2020nc",
            ),
            HDRType.UNKNOWN,
        ),
        (
            MediaProbe(bit_depth=10, color_transfer="smpte2084"),
            HDRType.UNKNOWN,
        ),
    ],
)
def test_classify_hdr(metadata, expected):
    assert classify_hdr(metadata) is expected


@pytest.mark.parametrize("profile", [7, 8])
def test_dovi_id6_base(profile):
    metadata = MediaProbe(
        bit_depth=10,
        color_transfer="smpte2084",
        color_primaries="bt2020",
        color_space="bt2020nc",
        dovi_profile=profile,
        dovi_bl_present=True,
        dovi_bl_signal_compatibility_id=6,
    )

    transcoder._require_supported_hdr(metadata)


@pytest.mark.parametrize(
    "metadata",
    [
        MediaProbe(
            bit_depth=10,
            dovi_profile=4,
            dovi_bl_present=True,
            dovi_bl_signal_compatibility_id=2,
        ),
        MediaProbe(
            bit_depth=10,
            dovi_profile=5,
            dovi_bl_present=True,
            dovi_bl_signal_compatibility_id=0,
        ),
    ],
)
def test_dovi_missing_base(metadata):
    with pytest.raises(RuntimeError, match="Dolby Vision-only"):
        transcoder._require_supported_hdr(metadata)


@pytest.mark.parametrize(
    (
        "width",
        "height",
        "sar",
        "rotation",
        "resolution",
        "display",
        "target",
        "needs_downscale",
        "needs_square_pixels",
    ),
    [
        (1920, 1080, None, 0, "720p", (1920, 1080), (1280, 720), True, False),
        (1920, 1080, None, 90, "1080p", (1080, 1920), (592, 1080), True, False),
        (
            720,
            576,
            (16, 15),
            0,
            "original",
            (768, 576),
            (768, 576),
            False,
            True,
        ),
        (
            720,
            576,
            (16, 15),
            90,
            "original",
            (576, 768),
            (576, 768),
            False,
            True,
        ),
        (
            720,
            576,
            (64, 45),
            0,
            "original",
            (1024, 576),
            (1024, 576),
            False,
            True,
        ),
        (1920, 1080, (1, 1), 0, "original", (1920, 1080), None, False, False),
    ],
)
def test_display_geometry(
    width,
    height,
    sar,
    rotation,
    resolution,
    display,
    target,
    needs_downscale,
    needs_square_pixels,
):
    context = TranscodeContext(
        options=TranscodeOptions(resolution=resolution),
        metadata=MediaProbe(
            width=width,
            height=height,
            sample_aspect_ratio=sar,
            rotation=rotation,
        ),
    )

    assert (context.display_width, context.display_height) == tuple(
        Fraction(value) for value in display
    )
    assert context.needs_downscale is needs_downscale
    assert context.needs_square_pixels is needs_square_pixels
    assert context.needs_scale is (target is not None)
    if target is None:
        assert context.scale_width is None
        assert context.scale_height is None
    else:
        assert (context.scale_width, context.scale_height) == tuple(
            str(value) for value in target
        )


@pytest.mark.parametrize(
    ("field_order", "rotation", "sar", "expected"),
    [
        (
            "tt",
            90,
            (16, 15),
            [
                "bwdif=mode=send_frame:parity=tff:deint=all",
                "setfield=prog",
                "transpose=clock",
                "scale=576:768",
                "setsar=1",
            ],
        ),
        (
            "bb",
            180,
            None,
            [
                "bwdif=mode=send_frame:parity=bff:deint=all",
                "setfield=prog",
                "transpose=clock",
                "transpose=clock",
            ],
        ),
        ("progressive", 270, None, ["transpose=cclock"]),
        ("unknown", 0, None, []),
    ],
)
def test_cpu_geometry_filters(field_order, rotation, sar, expected):
    context = TranscodeContext(
        options=TranscodeOptions(),
        metadata=MediaProbe(
            width=720,
            height=576,
            sample_aspect_ratio=sar,
            rotation=rotation,
            field_order=field_order,
        ),
    )

    assert base_module.cpu_geometry_filters(context) == expected


def _capabilities(
    *,
    encoders=(
        "aac",
        "h264_nvenc",
        "h264_qsv",
        "h264_vaapi",
        "h264_videotoolbox",
        "libx264",
    ),
    hwaccels=("cuda", "qsv", "vaapi", "videotoolbox"),
    filters=(
        "bwdif",
        "deinterlace_vaapi",
        "format",
        "hwupload",
        "hwupload_cuda",
        "scale",
        "scale_cuda",
        "scale_vaapi",
        "scale_vt",
        "setfield",
        "setsar",
        "tonemap",
        "tonemap_vaapi",
        "transpose",
        "transpose_vaapi",
        "transpose_vt",
        "vpp_qsv",
        "yadif_cuda",
        "zscale",
    ),
    encoder_options=(
        "crf",
        "forced-idr",
        "forced_idr",
        "mbbrc",
        "preset",
        "prio_speed",
        "profile",
        "qp",
        "rc_init_occupancy",
        "rc_mode",
    ),
    bsfs=("h264_metadata",),
    muxers=("hls", "mpegts"),
):
    return FFmpegCapabilities(
        executable="ffmpeg",
        encoders=frozenset(encoders),
        filters=frozenset(filters),
        hwaccels=frozenset(hwaccels),
        bsfs=frozenset(bsfs),
        muxers=frozenset(muxers),
        encoder_options=frozenset(encoder_options),
    )


def _hdr_context(
    hwaccel: HWAccelType | None = None,
    resolution: ResolutionLimit = "original",
    transfer: str = "smpte2084",
    capabilities: FFmpegCapabilities | None = None,
) -> TranscodeContext:
    return TranscodeContext(
        options=TranscodeOptions(hwaccel=hwaccel, resolution=resolution),
        metadata=MediaProbe(
            video_stream_index=0,
            audio_stream_index=1,
            avg_frame_rate=Fraction(24),
            r_frame_rate=Fraction(24),
            pixel_format="yuv420p10le",
            bit_depth=10,
            width=3840,
            height=2160,
            color_transfer=transfer,
            color_primaries="bt2020",
            color_space="bt2020nc",
        ),
        capabilities=capabilities,
    )


def _qsv_hdr_metadata(hdr_type: HDRType) -> MediaProbe:
    metadata = {
        HDRType.HDR10: MediaProbe(
            bit_depth=10,
            color_transfer="smpte2084",
            color_primaries="bt2020",
            color_space="bt2020nc",
        ),
        HDRType.HLG: MediaProbe(
            bit_depth=10,
            color_transfer="arib-std-b67",
            color_primaries="bt2020",
            color_space="bt2020nc",
        ),
        HDRType.HDR10_PLUS: MediaProbe(
            bit_depth=10,
            color_transfer="smpte2084",
            color_primaries="bt2020",
            color_space="bt2020nc",
            hdr10_plus=True,
        ),
        HDRType.DOVI_COMPATIBLE: MediaProbe(
            dovi_profile=8,
            dovi_bl_present=True,
            dovi_bl_signal_compatibility_id=1,
        ),
    }
    return metadata[hdr_type]


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
def test_software_hdr_filters(transfer):
    context = _hdr_context(transfer=transfer)

    assert get_hwaccel(None).video_filters(context) == [
        "zscale=transfer=linear:npl=100",
        "format=gbrpf32le",
        "tonemap=hable:desat=0",
        "zscale=primaries=bt709:transfer=bt709:matrix=bt709:range=tv",
        "format=yuv420p",
    ]


def test_hdr_bt709_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(transcoder, "_ffmpeg", AsyncMock(return_value="ffmpeg"))
    context = _hdr_context(resolution="720p")

    cmd = asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))

    vf = cmd[cmd.index("-vf") + 1]
    assert not vf.startswith("scale='")
    assert "tonemap=hable:desat=0" in vf
    assert "-color_primaries" not in cmd
    assert "-color_trc" not in cmd
    assert "-colorspace" not in cmd
    assert "-color_range" not in cmd
    assert cmd[cmd.index("-bsf:v") + 1] == (
        "h264_metadata=colour_primaries=1:transfer_characteristics=1:"
        "matrix_coefficients=1:video_full_range_flag=0"
    )


def test_hdr_optional_bsf(tmp_path):
    context = _hdr_context(capabilities=_capabilities(bsfs=()))

    cmd = asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))

    assert "-bsf:v" not in cmd


@pytest.mark.parametrize(
    ("capabilities", "missing"),
    [
        (
            _capabilities(
                encoders=(
                    "aac",
                    "h264_nvenc",
                    "h264_qsv",
                    "h264_vaapi",
                    "h264_videotoolbox",
                )
            ),
            "encoders: libx264",
        ),
        (_capabilities(encoders=("libx264",)), "encoders: aac"),
        (_capabilities(muxers=("mpegts",)), "muxers: hls"),
        (_capabilities(muxers=("hls",)), "muxers: mpegts"),
        (
            _capabilities(filters=("format", "tonemap")),
            "filters: zscale",
        ),
    ],
)
def test_required_capabilities(tmp_path, capabilities, missing):
    context = _hdr_context(capabilities=capabilities)

    with pytest.raises(RuntimeError, match=missing):
        asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))


def test_software_cmd(monkeypatch, tmp_path):
    monkeypatch.setattr(transcoder, "_ffmpeg", AsyncMock(return_value="ffmpeg"))
    context = TranscodeContext(
        options=TranscodeOptions(),
        metadata=MediaProbe(video_stream_index=0, audio_stream_index=1),
    )

    cmd = asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))

    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-crf") + 1] == "23"
    assert cmd[cmd.index("-hls_time") + 1] == "6"
    assert "-level" not in cmd
    assert cmd[cmd.index("-hls_flags") + 1] == "independent_segments"
    keyframe_index = cmd.index("-force_key_frames:0")
    assert cmd[keyframe_index : keyframe_index + 8] == [
        "-force_key_frames:0",
        "expr:if(isnan(prev_forced_t),1,gte(t,prev_forced_t+6))",
        "-flags:v:0",
        "+cgop",
        "-sc_threshold:v:0",
        "0",
        "-c:a",
        "aac",
    ]
    assert "-vf" not in cmd


@pytest.mark.parametrize(
    ("hwaccel", "device"),
    [
        (None, None),
        ("nvenc", "0"),
        ("qsv", "/dev/dri/renderD128"),
        ("vaapi", "/dev/dri/renderD128"),
        ("videotoolbox", None),
    ],
)
def test_command_rotation_metadata(monkeypatch, tmp_path, hwaccel, device):
    monkeypatch.setattr(transcoder, "_ffmpeg", AsyncMock(return_value="ffmpeg"))
    context = TranscodeContext(
        options=TranscodeOptions(hwaccel=hwaccel),
        metadata=MediaProbe(
            video_stream_index=0,
            width=1920,
            height=1080,
            rotation=90,
            pixel_format="yuv420p",
        ),
        capabilities=_capabilities(),
        hardware=(
            base_module.HardwareRuntime(device, False, False)
            if hwaccel is not None
            else None
        ),
    )

    cmd = asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))

    display_rotation_index = cmd.index("-display_rotation")
    assert cmd[display_rotation_index + 1] == "0"
    assert display_rotation_index < cmd.index("-i")
    assert cmd.index("-noautorotate") < cmd.index("-i")
    metadata_index = cmd.index("-metadata:s:v:0")
    assert cmd[metadata_index + 1] == "rotate=0"
    assert cmd[cmd.index("-c:v") + 1] == context.options.encoder


def test_interlaced_output(monkeypatch, tmp_path):
    monkeypatch.setattr(transcoder, "_ffmpeg", AsyncMock(return_value="ffmpeg"))
    context = TranscodeContext(
        options=TranscodeOptions(),
        metadata=MediaProbe(
            video_stream_index=0,
            width=1920,
            height=1080,
            field_order="tb",
        ),
        capabilities=_capabilities(),
    )

    cmd = asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))

    field_order_index = cmd.index("-field_order")
    assert cmd[field_order_index + 1] == "progressive"


def test_interlaced_bwdif_requirement(monkeypatch, tmp_path):
    monkeypatch.setattr(transcoder, "_ffmpeg", AsyncMock(return_value="ffmpeg"))
    capabilities = _capabilities(filters=_capabilities().filters - {"bwdif"})

    progressive = TranscodeContext(
        options=TranscodeOptions(),
        metadata=MediaProbe(
            video_stream_index=0,
            width=1920,
            height=1080,
            field_order="progressive",
        ),
        capabilities=capabilities,
    )
    asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, progressive))

    interlaced = replace(
        progressive,
        metadata=replace(progressive.metadata, field_order="tt"),
    )
    with pytest.raises(RuntimeError, match="filters: bwdif"):
        asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, interlaced))


def test_stream_mapping(tmp_path):
    context = TranscodeContext(
        options=TranscodeOptions(),
        metadata=MediaProbe(video_stream_index=3, audio_stream_index=1),
        capabilities=_capabilities(),
    )

    cmd = asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))

    maps = [cmd[index + 1] for index, value in enumerate(cmd) if value == "-map"]
    assert maps == ["0:3", "0:1"]
    assert "-an" not in cmd


def test_silent_video(tmp_path):
    context = TranscodeContext(
        options=TranscodeOptions(),
        metadata=MediaProbe(video_stream_index=2),
        capabilities=_capabilities(encoders=("libx264",)),
    )

    cmd = asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))

    maps = [cmd[index + 1] for index, value in enumerate(cmd) if value == "-map"]
    assert maps == ["0:2"]
    assert "-an" in cmd
    assert "-c:a" not in cmd


def test_audio_requires_aac(tmp_path):
    context = TranscodeContext(
        options=TranscodeOptions(),
        metadata=MediaProbe(video_stream_index=0, audio_stream_index=1),
        capabilities=_capabilities(encoders=("libx264",)),
    )

    with pytest.raises(RuntimeError, match="encoders: aac"):
        asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))


def test_missing_video(tmp_path):
    context = TranscodeContext(
        options=TranscodeOptions(),
        metadata=MediaProbe(audio_stream_index=1),
        capabilities=_capabilities(),
    )

    with pytest.raises(RuntimeError, match="no transcodable video stream"):
        asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))


@pytest.mark.parametrize(
    "hdr_type",
    [
        HDRType.HDR10,
        HDRType.HLG,
        HDRType.HDR10_PLUS,
        HDRType.DOVI_COMPATIBLE,
    ],
)
def test_qsv_hdr_decode(monkeypatch, hdr_type):
    device = "/dev/dri/renderD128"
    context = TranscodeContext(
        options=TranscodeOptions(hwaccel="qsv"),
        metadata=_qsv_hdr_metadata(hdr_type),
        capabilities=_capabilities(),
    )
    monkeypatch.setattr(
        qsv_module,
        "resolve_vaapi_device",
        AsyncMock(return_value=device),
    )

    assert asyncio.run(get_hwaccel("qsv").input_args(context)) == [
        "-init_hw_device",
        f"qsv=qs:hw,child_device={device},child_device_type=vaapi",
        "-filter_hw_device",
        "qs",
    ]


@pytest.mark.parametrize("missing", ["format", "hwupload", "tonemap", "zscale"])
def test_qsv_hdr_requirements(monkeypatch, tmp_path, missing):
    filters = {"format", "hwupload", "tonemap", "zscale"} - {missing}
    context = TranscodeContext(
        options=TranscodeOptions(hwaccel="qsv"),
        metadata=MediaProbe(
            video_stream_index=0,
            bit_depth=10,
            color_transfer="smpte2084",
            color_primaries="bt2020",
            color_space="bt2020nc",
        ),
        capabilities=_capabilities(filters=filters),
    )
    monkeypatch.setattr(
        qsv_module,
        "resolve_vaapi_device",
        AsyncMock(return_value="/dev/dri/renderD128"),
    )

    with pytest.raises(RuntimeError, match=rf"filters: {missing}"):
        asyncio.run(transcoder._build_hls_cmd("input.mkv", tmp_path, context))


@pytest.mark.parametrize(
    ("quality", "bitrate", "bufsize"),
    [
        ("low", "1500k", "3000k"),
        ("medium", "3000k", "6000k"),
        ("high", "6000k", "12000k"),
    ],
)
def test_vaapi_auto_rate_control(quality, bitrate, bufsize):
    context = TranscodeContext(
        options=TranscodeOptions(hwaccel="vaapi", quality=quality)
    )

    assert get_hwaccel("vaapi").encoder_args(context) == [
        "-b:v",
        bitrate,
        "-maxrate",
        bitrate,
        "-bufsize",
        bufsize,
    ]


def test_vaapi_hdr_scale_probe(monkeypatch, tmp_path):
    media = tmp_path / "input.mkv"
    media.write_bytes(b"video")
    device = "/dev/dri/renderD128"
    strategy = get_hwaccel("vaapi")
    monkeypatch.setattr(base_module, "require_hardware_encoder", AsyncMock())
    monkeypatch.setattr(
        base_module,
        "probe_hardware_decode",
        AsyncMock(return_value=True),
    )
    probe_transform = AsyncMock(return_value=True)
    monkeypatch.setattr(base_module, "probe_hardware_transform", probe_transform)
    monkeypatch.setattr(
        strategy,
        "resolve_device",
        AsyncMock(return_value=device),
    )
    context = _hdr_context(
        hwaccel="vaapi",
        resolution="720p",
        capabilities=_capabilities(),
    )
    context.metadata = replace(
        context.metadata,
        codec="hevc",
        profile="Main 10",
    )
    context.media_path = str(media)

    context.hardware = asyncio.run(strategy.prepare_hardware(context))

    assert context.hardware == base_module.HardwareRuntime(device, True, True)
    transform_call = probe_transform.await_args
    assert transform_call is not None
    transform_args = transform_call.args[-1]
    assert transform_args[transform_args.index("-vf") + 1] == (
        "scale_vaapi=w=1280:h=720,setsar=1,hwdownload,format=p010le"
    )
    assert strategy.video_filters(context) == [
        "scale_vaapi=w=1280:h=720",
        "setsar=1",
        "tonemap_vaapi=format=nv12:p=bt709:t=bt709:m=bt709",
    ]


def test_vaapi_hdr_fallback(monkeypatch):
    context = _hdr_context(
        hwaccel="vaapi",
        capabilities=_capabilities(hwaccels=()),
    )
    monkeypatch.setattr(
        vaapi_module,
        "resolve_vaapi_device",
        AsyncMock(return_value="/dev/dri/renderD128"),
    )

    args = asyncio.run(get_hwaccel("vaapi").input_args(context))
    filters = get_hwaccel("vaapi").video_filters(context)

    assert "-hwaccel" not in args
    assert filters[:2] == ["format=p010", "hwupload"]


def test_vaapi_device(monkeypatch):
    monkeypatch.setattr(
        vaapi_module, "resolve_vaapi_device", AsyncMock(return_value=None)
    )
    context = TranscodeContext(options=TranscodeOptions(hwaccel="vaapi"))

    with pytest.raises(RuntimeError, match="DRM render device"):
        asyncio.run(get_hwaccel("vaapi").input_args(context))


@pytest.mark.parametrize(
    "metadata",
    [
        MediaProbe(),
        MediaProbe(
            avg_frame_rate=Fraction(6075, 271),
            r_frame_rate=Fraction(15),
        ),
    ],
)
def test_unstable_framerate_gop(metadata):
    context = TranscodeContext(
        options=TranscodeOptions(hwaccel="nvenc"),
        metadata=metadata,
    )

    args = get_hwaccel("nvenc").keyframe_args(context)

    assert args == [
        "-force_key_frames:0",
        "expr:if(isnan(prev_forced_t),1,gte(t,prev_forced_t+6))",
        "-flags:v:0",
        "+cgop",
    ]


@pytest.mark.parametrize(
    ("hwaccel", "option"),
    [("nvenc", "forced-idr"), ("qsv", "forced_idr")],
)
def test_forced_idr_requirement(hwaccel, option):
    context = TranscodeContext(
        options=TranscodeOptions(hwaccel=hwaccel),
        capabilities=_capabilities(encoder_options=()),
    )

    with pytest.raises(RuntimeError, match=rf"encoder options: {option}"):
        get_hwaccel(hwaccel).encoder_args(context)


@pytest.mark.parametrize(
    ("hwaccel", "available", "missing"),
    [
        (None, (), {"-preset", "-crf", "-profile:v"}),
        ("nvenc", ("forced-idr",), {"-preset"}),
        (
            "qsv",
            ("forced_idr",),
            {"-preset", "-mbbrc", "-rc_init_occupancy"},
        ),
        ("vaapi", (), {"-rc_mode", "-qp"}),
        ("videotoolbox", (), {"-prio_speed"}),
    ],
)
def test_optional_encoder_options(hwaccel, available, missing):
    context = TranscodeContext(
        options=TranscodeOptions(hwaccel=hwaccel),
        capabilities=_capabilities(encoder_options=available),
    )

    args = get_hwaccel(hwaccel).encoder_args(context)

    assert missing.isdisjoint(args)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quality": "/tmp/outside"},
        {"resolution": "../outside"},
        {"hwaccel": "invalid"},
    ],
)
def test_invalid_options(kwargs):
    with pytest.raises(ValueError):
        TranscodeOptions(**kwargs)


def test_rechecks_completion(monkeypatch, tmp_path):
    lock = object()
    complete = Mock(side_effect=[False, True])
    cleanup = Mock(side_effect=AssertionError("cleanup called"))
    release = Mock()
    options = TranscodeOptions()

    monkeypatch.setattr(transcoder, "output_dir", lambda _hash, _profile: tmp_path)
    monkeypatch.setattr(transcoder, "is_complete", complete)
    monkeypatch.setattr(transcoder, "_acquire_lock", lambda _path: lock)
    monkeypatch.setattr(transcoder, "cleanup_stale_hls", cleanup)
    monkeypatch.setattr(transcoder, "_release_lock", release)

    result = asyncio.run(transcoder.ensure_transcode("input.mkv", "hash", options))

    assert result == ("hash", options.profile)
    assert complete.call_count == 2
    cleanup.assert_not_called()
    release.assert_called_once_with(lock)


def test_setup_failure_cleanup(monkeypatch, tmp_path):
    events = []

    async def communicate():
        events.append("wait")
        return b"", b""

    lock = object()
    proc = SimpleNamespace(
        pid=123,
        returncode=None,
        stderr=None,
        terminate=Mock(side_effect=lambda: events.append("terminate")),
        kill=Mock(),
        communicate=AsyncMock(side_effect=communicate),
    )
    release = Mock(side_effect=lambda _lock: events.append("release"))

    monkeypatch.setattr(transcoder, "output_dir", lambda _hash, _profile: tmp_path)
    monkeypatch.setattr(transcoder, "is_complete", Mock(return_value=False))
    monkeypatch.setattr(transcoder, "_acquire_lock", lambda _path: lock)
    monkeypatch.setattr(transcoder, "cleanup_stale_hls", Mock())
    monkeypatch.setattr(
        transcoder,
        "probe_media",
        AsyncMock(
            return_value=MediaProbe(
                video_stream_index=0,
                audio_stream_index=1,
                duration=60.0,
                width=1920,
                height=1080,
            )
        ),
    )
    monkeypatch.setattr(
        transcoder, "_build_hls_cmd", AsyncMock(return_value=["ffmpeg"])
    )
    monkeypatch.setattr(transcoder, "_ffmpeg", AsyncMock(return_value="ffmpeg"))
    monkeypatch.setattr(
        transcoder,
        "load_ffmpeg_capabilities",
        AsyncMock(return_value=_capabilities()),
    )
    monkeypatch.setattr(
        transcoder.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )
    monkeypatch.setattr(
        transcoder, "register_task", AsyncMock(side_effect=RuntimeError("store failed"))
    )
    monkeypatch.setattr(transcoder, "_release_lock", release)

    with pytest.raises(RuntimeError, match="store failed"):
        asyncio.run(
            transcoder.ensure_transcode("input.mkv", "hash", TranscodeOptions())
        )

    assert events == ["terminate", "wait", "release"]
    proc.kill.assert_not_called()


def test_preflight_failure_lock(monkeypatch, tmp_path):
    lock = object()
    release = Mock()
    create = AsyncMock(side_effect=AssertionError("transcode process started"))
    capabilities = _capabilities(encoders=("aac",))

    monkeypatch.setattr(transcoder, "output_dir", lambda _hash, _profile: tmp_path)
    monkeypatch.setattr(transcoder, "is_complete", Mock(return_value=False))
    monkeypatch.setattr(transcoder, "_acquire_lock", lambda _path: lock)
    monkeypatch.setattr(transcoder, "cleanup_stale_hls", Mock())
    monkeypatch.setattr(
        transcoder,
        "probe_media",
        AsyncMock(
            return_value=MediaProbe(
                video_stream_index=0,
                audio_stream_index=1,
                duration=60.0,
                width=1920,
                height=1080,
            )
        ),
    )
    monkeypatch.setattr(transcoder, "_ffmpeg", AsyncMock(return_value="ffmpeg"))
    monkeypatch.setattr(
        transcoder,
        "load_ffmpeg_capabilities",
        AsyncMock(return_value=capabilities),
    )
    monkeypatch.setattr(transcoder.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(transcoder, "_release_lock", release)

    with pytest.raises(RuntimeError, match="encoders: libx264"):
        asyncio.run(
            transcoder.ensure_transcode("input.mkv", "hash", TranscodeOptions())
        )

    create.assert_not_awaited()
    release.assert_called_once_with(lock)


@pytest.mark.parametrize(
    ("metadata", "options", "message"),
    [
        (
            MediaProbe(width=1920, height=1080, rotation=45),
            TranscodeOptions(),
            "Unsupported video rotation: 45",
        ),
        (
            MediaProbe(rotation=90),
            TranscodeOptions(),
            "requires valid width and height",
        ),
        (
            MediaProbe(),
            TranscodeOptions(resolution="720p"),
            "requires valid width and height",
        ),
        (
            MediaProbe(sample_aspect_ratio=(4, 3)),
            TranscodeOptions(),
            "requires valid width and height",
        ),
    ],
)
def test_invalid_geometry(metadata, options, message):
    with pytest.raises(RuntimeError, match=message):
        transcoder._require_supported_geometry(metadata, options)


def test_hardware_failure_lock(monkeypatch, tmp_path):
    lock = object()
    release = Mock()
    build = AsyncMock(side_effect=AssertionError("command built"))
    create = AsyncMock(side_effect=AssertionError("transcode process started"))
    register = AsyncMock(side_effect=AssertionError("task registered"))
    metadata = MediaProbe(
        video_stream_index=0,
        codec="h264",
        profile="High",
        bit_depth=8,
        pixel_format="yuv420p",
    )

    monkeypatch.setattr(transcoder, "output_dir", lambda _hash, _profile: tmp_path)
    monkeypatch.setattr(transcoder, "is_complete", Mock(return_value=False))
    monkeypatch.setattr(transcoder, "_acquire_lock", lambda _path: lock)
    monkeypatch.setattr(transcoder, "cleanup_stale_hls", Mock())
    monkeypatch.setattr(transcoder, "probe_media", AsyncMock(return_value=metadata))
    monkeypatch.setattr(
        transcoder,
        "_prepare_hardware",
        AsyncMock(side_effect=RuntimeError("encoder unavailable")),
        raising=False,
    )
    monkeypatch.setattr(transcoder, "_build_hls_cmd", build)
    monkeypatch.setattr(transcoder, "_ffmpeg", AsyncMock(return_value="ffmpeg"))
    monkeypatch.setattr(
        transcoder,
        "load_ffmpeg_capabilities",
        AsyncMock(return_value=_capabilities()),
    )
    monkeypatch.setattr(transcoder.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(transcoder, "register_task", register)
    monkeypatch.setattr(transcoder, "_release_lock", release)

    with pytest.raises(RuntimeError, match="encoder unavailable"):
        asyncio.run(
            transcoder.ensure_transcode(
                "input.mkv",
                "hash",
                TranscodeOptions(hwaccel="nvenc"),
            )
        )

    build.assert_not_awaited()
    create.assert_not_awaited()
    register.assert_not_awaited()
    release.assert_called_once_with(lock)


def test_cleanup_timeout():
    proc = SimpleNamespace(
        returncode=None,
        terminate=Mock(),
        kill=Mock(),
        communicate=AsyncMock(side_effect=[TimeoutError, (b"", b"")]),
    )

    asyncio.run(transcoder._terminate_ffmpeg(cast(asyncio.subprocess.Process, proc)))

    proc.terminate.assert_called_once_with()
    proc.kill.assert_called_once_with()
    assert proc.communicate.await_count == 2


def test_shutdown_monitors(monkeypatch):
    finish = AsyncMock()
    release = Mock()

    monkeypatch.setattr(transcoder, "finish_task", finish)
    monkeypatch.setattr(transcoder, "_release_lock", release)

    async def run():
        started = asyncio.Event()
        blocker = asyncio.Event()

        async def read(_size):
            started.set()
            await blocker.wait()

        proc = SimpleNamespace(
            pid=123,
            returncode=None,
            stderr=SimpleNamespace(read=AsyncMock(side_effect=read)),
            terminate=Mock(),
            kill=Mock(),
            communicate=AsyncMock(return_value=(b"", b"")),
        )
        lock = object()
        completion = transcoder._start_monitor(
            cast(asyncio.subprocess.Process, proc), cast(FileLock, lock), "task"
        )
        task = next(iter(transcoder._MONITOR_TASKS))
        assert task in transcoder._MONITOR_TASKS

        await started.wait()
        await transcoder.shutdown_monitors()
        return task, completion, proc, lock

    task, completion, proc, lock = asyncio.run(run())

    assert task.cancelled()
    assert completion.result().state == "stopped"
    assert not transcoder._MONITOR_TASKS
    proc.terminate.assert_called_once_with()
    finish.assert_awaited_once_with("task", 255)
    release.assert_called_once_with(lock)


def test_stderr_redaction():
    detail = transcoder._stderr_detail(
        (
            b"Cannot open /private/media/secret.mkv\n"
            b"/output/hash/segment_000000.ts failed\n"
        ),
        {
            "/private/media/secret.mkv": "<input>",
            "secret.mkv": "<input>",
            "/output/hash": "<output>",
        },
    )

    assert detail == "Cannot open <input>\n<output>/segment_000000.ts failed"


def test_stderr_bounds():
    data = b"".join(f"error-{index:02d} {'x' * 500}\n".encode() for index in range(40))

    detail = transcoder._stderr_detail(data)

    assert detail is not None
    assert len(detail.encode()) <= 8 * 1024
    assert len(detail.splitlines()) <= 24
    assert "error-39" in detail
    assert "error-00" not in detail


def test_monitor_tail(monkeypatch):
    read = AsyncMock(side_effect=[b"a" * 400, b"b" * 400, b""])
    proc = SimpleNamespace(
        pid=123,
        returncode=1,
        stderr=SimpleNamespace(read=read),
        wait=AsyncMock(),
    )
    finish = AsyncMock()
    release = Mock()

    monkeypatch.setattr(transcoder, "finish_task", finish)
    monkeypatch.setattr(transcoder, "_release_lock", release)
    monkeypatch.setattr(transcoder.logger, "error", Mock())

    result = asyncio.run(
        transcoder._monitor_ffmpeg(
            cast(asyncio.subprocess.Process, proc),
            cast(FileLock, object()),
            "task",
        )
    )

    assert read.await_count == 3
    assert result == transcoder.FFmpegCompletion.from_exit(1, "a" * 400 + "b" * 400)
    finish.assert_awaited_once_with("task", 1, "a" * 400 + "b" * 400)


def test_startup_failure_detail(monkeypatch, tmp_path):
    source = "/private/media/secret.mkv"
    out_dir = tmp_path / "hash" / "profile"
    lock = object()
    proc = SimpleNamespace(
        pid=123,
        returncode=1,
        stderr=SimpleNamespace(
            read=AsyncMock(
                side_effect=[
                    (
                        f"Cannot create compression session for {source}\n"
                        f"{out_dir}/segment_000000.ts was not written\n"
                    ).encode(),
                    b"",
                ]
            )
        ),
        wait=AsyncMock(),
    )
    finish = AsyncMock()
    release = Mock()
    options = TranscodeOptions()

    monkeypatch.setattr(transcoder, "output_dir", lambda _hash, _profile: out_dir)
    monkeypatch.setattr(transcoder, "is_complete", Mock(return_value=False))
    monkeypatch.setattr(transcoder, "_acquire_lock", lambda _path: lock)
    monkeypatch.setattr(transcoder, "cleanup_stale_hls", Mock())
    monkeypatch.setattr(
        transcoder,
        "probe_media",
        AsyncMock(return_value=MediaProbe(video_stream_index=0, duration=60.0)),
    )
    monkeypatch.setattr(
        transcoder, "_build_hls_cmd", AsyncMock(return_value=["ffmpeg"])
    )
    monkeypatch.setattr(transcoder, "_ffmpeg", AsyncMock(return_value="ffmpeg"))
    monkeypatch.setattr(
        transcoder,
        "load_ffmpeg_capabilities",
        AsyncMock(return_value=_capabilities()),
    )
    monkeypatch.setattr(
        transcoder.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    monkeypatch.setattr(
        transcoder, "register_task", AsyncMock(return_value="hash:profile")
    )
    monkeypatch.setattr(transcoder, "finish_task", finish)
    monkeypatch.setattr(transcoder, "wait_segment", AsyncMock(return_value=False))
    monkeypatch.setattr(transcoder, "_release_lock", release)
    monkeypatch.setattr(transcoder.logger, "error", Mock())

    async def run():
        try:
            return await transcoder.ensure_transcode(source, "hash", options)
        finally:
            await asyncio.gather(
                *tuple(transcoder._MONITOR_TASKS), return_exceptions=True
            )

    with pytest.raises(KaloscopeException) as caught:
        asyncio.run(run())

    assert type(caught.value) is KaloscopeException
    message = str(caught.value)
    assert "FFmpeg failed with code 1" in message
    assert "Cannot create compression session" in message
    assert source not in message
    assert Path(source).name not in message
    assert str(out_dir) not in message
    finish.assert_awaited_once_with(
        "hash:profile",
        1,
        (
            "Cannot create compression session for <input>\n"
            "<output>/segment_000000.ts was not written"
        ),
    )
    release.assert_called_once_with(lock)


def test_timeout_keeps_lock(monkeypatch, tmp_path):
    lock = object()
    proc = SimpleNamespace(pid=123, returncode=None, stderr=None)
    release = Mock()

    monkeypatch.setattr(
        transcoder, "output_dir", lambda _hash, profile: tmp_path / profile
    )
    monkeypatch.setattr(transcoder, "_acquire_lock", lambda _path: lock)
    monkeypatch.setattr(
        transcoder,
        "probe_media",
        AsyncMock(
            return_value=MediaProbe(
                video_stream_index=0,
                audio_stream_index=1,
                duration=60.0,
                width=1920,
                height=1080,
            )
        ),
    )
    monkeypatch.setattr(
        transcoder, "_build_hls_cmd", AsyncMock(return_value=["ffmpeg"])
    )
    monkeypatch.setattr(transcoder, "_ffmpeg", AsyncMock(return_value="ffmpeg"))
    monkeypatch.setattr(
        transcoder,
        "load_ffmpeg_capabilities",
        AsyncMock(return_value=_capabilities()),
    )
    monkeypatch.setattr(
        transcoder.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )
    monkeypatch.setattr(
        transcoder, "register_task", AsyncMock(return_value="hash:profile")
    )
    monkeypatch.setattr(transcoder, "wait_segment", AsyncMock(return_value=False))
    monkeypatch.setattr(transcoder, "_release_lock", release)
    monkeypatch.setattr(transcoder, "_start_monitor", Mock())

    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(
            transcoder.ensure_transcode(
                "input.mkv", "hash", TranscodeOptions(resolution="720p")
            )
        )

    release.assert_not_called()


def test_monitor_lock(monkeypatch, tmp_path):
    proc = SimpleNamespace(stderr=None, returncode=0, wait=AsyncMock())
    lock = SimpleNamespace(lock_file=str(tmp_path / ".lock"))
    release = Mock()

    monkeypatch.setattr(
        transcoder, "finish_task", AsyncMock(side_effect=RuntimeError("store failed"))
    )
    monkeypatch.setattr(transcoder, "_release_lock", release)

    with pytest.raises(RuntimeError, match="store failed"):
        asyncio.run(
            transcoder._monitor_ffmpeg(
                cast(asyncio.subprocess.Process, proc),
                cast(FileLock, lock),
                "task",
            )
        )

    release.assert_called_once_with(lock)


def test_finish_lock(monkeypatch, tmp_path):
    lock = _Lock()
    store = {
        "task": {
            "state": "running",
            "out_dir": str(tmp_path),
            "started_at": "2026-01-01",
            "pid": 123,
        }
    }

    def remove_endlist(_out_dir):
        assert lock.locked is False

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, lock))
    monkeypatch.setattr(tasks, "remove_endlist", remove_endlist)

    asyncio.run(tasks.finish_task("task", 1, "failed"))

    assert store["task"]["state"] == "error"


def test_stop_lock(monkeypatch, tmp_path):
    lock = _Lock()
    store = {
        "task": {
            "state": "running",
            "out_dir": str(tmp_path),
            "started_at": "2026-01-01",
            "pid": 123,
            "process_start_id": "original-process",
        }
    }

    def kill(_pid, _signal):
        assert lock.locked is False

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, lock))
    monkeypatch.setattr(
        tasks, "_process_start_id", AsyncMock(return_value="original-process")
    )
    monkeypatch.setattr(tasks.os, "kill", kill)

    result = asyncio.run(tasks.stop_tasks(["task"]))

    assert result == ["task"]
    assert store["task"]["state"] == "stopping"


def test_delete_lock(monkeypatch, tmp_path):
    lock = _Lock()
    out_dir = tmp_path / "hash" / "profile"
    store = {
        "hash:profile": {
            "state": "finished",
            "out_dir": str(out_dir),
            "started_at": "2026-01-01",
            "pid": 123,
        }
    }

    def delete_output(_hash, _profile, root=None):
        assert lock.locked is False
        assert root == tmp_path
        return True

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, lock))
    monkeypatch.setattr(tasks, "delete_output", delete_output)

    result = asyncio.run(tasks.delete_tasks(["hash:profile"]))

    assert result == ["hash:profile"]
    assert not store


def test_delete_locked_output(monkeypatch, tmp_path):
    out_dir = tmp_path / "hash" / "profile"
    out_dir.mkdir(parents=True)
    playlist = out_dir / "index.m3u8"
    playlist.write_text("#EXTM3U\n")
    store = {
        "hash:profile": {
            **_runtime_task(out_dir, tasks.TaskState.FINISHED),
            "out_dir": str(out_dir),
        }
    }
    lock = transcoder._acquire_lock(out_dir)
    assert lock is not None

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, _Lock()))

    try:
        result = asyncio.run(tasks.delete_tasks(["hash:profile"]))
    finally:
        transcoder._release_lock(lock)

    assert result == []
    assert "hash:profile" in store
    assert playlist.is_file()


def test_delete_replacement(monkeypatch, tmp_path):
    lock = _Lock()
    original = {
        "state": "finished",
        "out_dir": str(tmp_path / "hash" / "profile"),
        "started_at": "2026-01-01",
        "pid": 123,
    }
    replacement = {
        "state": "running",
        "out_dir": original["out_dir"],
        "started_at": "2026-01-02",
        "pid": 456,
    }
    store = {"hash:profile": original}

    def delete_output(_hash, _profile, root=None):
        assert lock.locked is False
        store["hash:profile"] = replacement
        return True

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, lock))
    monkeypatch.setattr(tasks, "delete_output", delete_output)

    asyncio.run(tasks.delete_tasks(["hash:profile"]))

    assert store["hash:profile"] is replacement


@pytest.mark.parametrize(
    ("state", "returncode", "expected"),
    [
        (tasks.TaskState.RUNNING, 0, tasks.TaskState.FINISHED),
        (tasks.TaskState.RUNNING, 255, tasks.TaskState.STOPPED),
        (tasks.TaskState.STOPPING, 1, tasks.TaskState.STOPPED),
    ],
)
def test_finish_states(monkeypatch, tmp_path, state, returncode, expected):
    store = {"task": _runtime_task(tmp_path, state)}
    remove = Mock()

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, _Lock()))
    monkeypatch.setattr(tasks, "remove_endlist", remove)

    asyncio.run(tasks.finish_task("task", returncode))

    assert store["task"]["state"] == expected
    assert store["task"]["finished_at"] is not None
    if returncode == 0:
        remove.assert_not_called()
    else:
        remove.assert_called_once_with(str(tmp_path))


def test_stop_reused_pid(monkeypatch, tmp_path):
    store = {"task": _runtime_task(tmp_path)}
    kill = Mock()

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, _Lock()))
    monkeypatch.setattr(
        tasks,
        "_process_start_id",
        AsyncMock(return_value="replacement-process"),
        raising=False,
    )
    monkeypatch.setattr(tasks.os, "kill", kill)
    monkeypatch.setattr(tasks, "is_complete", Mock(return_value=False))
    monkeypatch.setattr(tasks, "remove_endlist", Mock())

    result = asyncio.run(tasks.stop_tasks(["task"]))

    assert result == ["task"]
    assert store["task"]["state"] == tasks.TaskState.STOPPED
    assert store["task"]["finished_at"] is not None
    kill.assert_not_called()


def test_stop_missing_process_start(monkeypatch, tmp_path):
    task = _runtime_task(tmp_path)
    task["process_start_id"] = None
    store = {"task": task}
    kill = Mock()

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, _Lock()))
    monkeypatch.setattr(tasks.os, "kill", kill)

    with pytest.raises(RuntimeError, match="Cannot safely identify"):
        asyncio.run(tasks.stop_tasks(["task"]))

    assert store["task"]["state"] == tasks.TaskState.RUNNING
    kill.assert_not_called()


def test_stop_windows_identity(monkeypatch, tmp_path):
    task = _runtime_task(tmp_path)
    task["process_start_id"] = None
    store = {"task": task}
    process_start_id = AsyncMock()
    kill = Mock()

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, _Lock()))
    monkeypatch.setattr(tasks, "_process_start_id", process_start_id)
    monkeypatch.setattr(tasks.sys, "platform", "win32")
    monkeypatch.delattr(tasks.signal, "SIGKILL")
    monkeypatch.setattr(tasks.os, "kill", kill)

    result = asyncio.run(tasks.stop_tasks(["task"]))

    assert result == ["task"]
    assert store["task"]["state"] == tasks.TaskState.STOPPING
    process_start_id.assert_not_awaited()
    kill.assert_called_once_with(123, tasks.signal.SIGTERM)


def test_stop_rollback(monkeypatch, tmp_path):
    store = {"task": _runtime_task(tmp_path)}

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, _Lock()))
    monkeypatch.setattr(
        tasks, "_process_start_id", AsyncMock(return_value="original-process")
    )
    monkeypatch.setattr(tasks.os, "kill", Mock(side_effect=PermissionError))

    with pytest.raises(PermissionError):
        asyncio.run(tasks.stop_tasks(["task"]))

    assert store["task"]["state"] == tasks.TaskState.RUNNING


def test_stop_restores_pending(monkeypatch, tmp_path):
    store = {}
    for pid in (1, 2, 3):
        task = _runtime_task(tmp_path / str(pid))
        task["pid"] = pid
        store[str(pid)] = task

    killed = []

    def kill(pid, _signal):
        killed.append(pid)
        if pid == 2:
            raise PermissionError

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, _Lock()))
    monkeypatch.setattr(
        tasks, "_process_start_id", AsyncMock(return_value="original-process")
    )
    monkeypatch.setattr(tasks.os, "kill", kill)

    with pytest.raises(PermissionError):
        asyncio.run(tasks.stop_tasks(["1", "2", "3"]))

    assert killed == [1, 2]
    assert store["1"]["state"] == tasks.TaskState.STOPPING
    assert store["2"]["state"] == tasks.TaskState.RUNNING
    assert store["3"]["state"] == tasks.TaskState.RUNNING


@pytest.mark.parametrize("state", [tasks.TaskState.RUNNING, tasks.TaskState.STOPPING])
def test_delete_active(monkeypatch, tmp_path, state):
    store = {"hash:profile": _runtime_task(tmp_path, state)}
    delete = Mock()

    monkeypatch.setattr(tasks, "_task_store", lambda: (store, _Lock()))
    monkeypatch.setattr(tasks, "delete_output", delete)

    result = asyncio.run(tasks.delete_tasks(["hash:profile"]))

    assert result == []
    assert "hash:profile" in store
    delete.assert_not_called()
