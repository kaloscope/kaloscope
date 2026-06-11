import os
import ssl
from typing import cast

import httpx
import sanic.mixins.startup as startup
from sanic import Sanic
from sanic.constants import LocalCertCreator
from sanic.http.tls.creators import CertCreator
from sanic.worker.manager import WorkerManager

# patch the WorkerManager to allow longer startup time
WorkerManager.THRESHOLD = 600  # type: ignore


def _patched_get_ssl_context(app: Sanic, ssl: ssl.SSLContext | None) -> ssl.SSLContext:
    """Patched version of get_ssl_context that allows self-signed TLS in PROD mode.

    Args:
        app: The Sanic application instance.
        ssl: An optional SSLContext. If provided, it will be used directly.

    Returns:
        The SSLContext to be used for TLS connections.
    """
    if ssl:
        return ssl

    creator = CertCreator.select(
        app,
        cast(LocalCertCreator, app.config.LOCAL_CERT_CREATOR),
        app.config.LOCAL_TLS_KEY,
        app.config.LOCAL_TLS_CERT,
    )
    # prefer TLS_HOSTNAME env variable over app config
    hostname = os.environ.get("TLS_HOSTNAME") or app.config.LOCALHOST
    context = creator.generate_cert(hostname)
    return context


startup.get_ssl_context = _patched_get_ssl_context  # type: ignore


def _patch_hishel_headers():
    """Patch hishel's `_httpx_to_internal` and `_internal_to_httpx` to
    preserve multi-value `Set-Cookie` headers through the cache layer.

    hishel's `Headers.__getitem__` joins values with `, ` which RFC 7230
    explicitly forbids for `Set-Cookie`.
    """
    import hishel._async_httpx as _async
    from hishel import Response as _HishelResponse
    from hishel._core._headers import Headers as _HishelHeaders

    def _httpx_to_hishel_headers(httpx_headers) -> _HishelHeaders:
        raw: dict[str, list[str]] = {}
        for k, v in httpx_headers.multi_items():
            raw.setdefault(k.lower(), []).append(v)
        raw.pop("transfer-encoding", None)
        return _HishelHeaders(raw)

    def _hishel_to_header_tuples(h: _HishelHeaders) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for key in h:
            for v in h.get_list(key) or []:
                pairs.append((key, v))
        return pairs

    _orig_httpx_to_internal = _async._httpx_to_internal

    def _httpx_to_internal(value):
        if not isinstance(value, httpx.Response):
            return _orig_httpx_to_internal(value)

        headers = _httpx_to_hishel_headers(value.headers)

        if not value.is_stream_consumed:
            return _HishelResponse(
                status_code=value.status_code,
                headers=headers,
                stream=value.aiter_raw(chunk_size=131072),  # 128 KB
                metadata={},
            )

        from hishel._utils import make_async_iterator

        stream = make_async_iterator([value.content])
        if "content-encoding" in value.headers:
            filtered = {
                k: list(headers.get_list(k) or [])
                for k in headers
                if k != "content-encoding"
            }
            filtered["content-length"] = [str(len(value.content))]
            headers = _HishelHeaders(filtered)
        return _HishelResponse(
            status_code=value.status_code,
            headers=headers,
            stream=stream,
            metadata={},
        )

    _orig_internal_to_httpx = _async._internal_to_httpx

    def _internal_to_httpx(value):
        if not isinstance(value, _HishelResponse):
            return _orig_internal_to_httpx(value)

        from hishel._async_httpx import _IteratorStream

        return httpx.Response(
            status_code=value.status_code,
            headers=_hishel_to_header_tuples(value.headers),
            stream=_IteratorStream(value._aiter_stream()),
            extensions=value.metadata,
        )

    _async._httpx_to_internal = _httpx_to_internal
    _async._internal_to_httpx = _internal_to_httpx


_patch_hishel_headers()
