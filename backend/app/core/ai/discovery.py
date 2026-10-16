"""Discover model IDs through provider catalogs without exposing upstream errors."""

import asyncio
import json

import httpx

from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.ai import AIProviderPreset


async def fetch_models(
    client: httpx.AsyncClient,
    base_url: str,
    preset: AIProviderPreset | None,
    api_key: str | None,
) -> list[str]:
    """Fetch a bounded model catalog using a credential-isolated network client.

    Args:
        client: Non-caching client retaining configured proxy and DNS rules.
        base_url: Validated API base URL without a trailing slash.
        preset: Provider preset, used for Anthropic headers and pagination.
        api_key: Resolved credential, or `None` for a keyless connection.

    Returns:
        Sorted, unique model IDs; an empty catalog is valid. Discovery does not
        guarantee that a model supports Chat Completions.

    Raises:
        KaloscopeException: If authentication, networking, or catalog parsing fails.
            Upstream bodies and credentials are never included in the error.
    """
    anthropic = preset == AIProviderPreset.ANTHROPIC
    headers = {"Accept": "application/json", "Cache-Control": "no-cache, no-store"}
    if anthropic:
        headers["anthropic-version"] = "2023-06-01"
    if api_key:
        if anthropic:
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    params = {"limit": "1000"} if anthropic else {}
    models: set[str] = set()
    cursors: set[str] = set()
    total_bytes = 0
    try:
        async with asyncio.timeout(20):
            for _ in range(20):
                async with client.stream(
                    "GET",
                    f"{base_url}/models",
                    headers=headers,
                    params=params,
                    timeout=10,
                    follow_redirects=False,
                ) as response:
                    if not response.is_success:
                        code = {
                            401: ErrorCode.AI_MODELS_AUTH_FAILED,
                            403: ErrorCode.AI_MODELS_AUTH_FAILED,
                            404: ErrorCode.AI_MODELS_UNSUPPORTED,
                            405: ErrorCode.AI_MODELS_UNSUPPORTED,
                            501: ErrorCode.AI_MODELS_UNSUPPORTED,
                            429: ErrorCode.AI_MODELS_RATE_LIMITED,
                        }.get(response.status_code, ErrorCode.AI_MODELS_FETCH_FAILED)
                        raise KaloscopeException(code, status_code=502, quiet=True)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > 8 * 1024 * 1024:
                            raise ValueError
                        body.extend(chunk)

                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("data"), list
                ):
                    raise ValueError
                for item in payload["data"]:
                    if not isinstance(item, dict) or not isinstance(
                        item.get("id"), str
                    ):
                        raise ValueError
                    model = item["id"].strip()
                    if not 1 <= len(model) <= 255 or any(
                        ord(char) < 32 or ord(char) == 127 for char in model
                    ):
                        raise ValueError
                    models.add(model)
                if len(models) > 10000:
                    raise ValueError
                if not anthropic or not payload.get("has_more"):
                    return sorted(models)
                cursor = payload.get("last_id")
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise ValueError
                cursors.add(cursor)
                params["after_id"] = cursor
            raise ValueError
    except (TimeoutError, httpx.TimeoutException):
        raise KaloscopeException(
            ErrorCode.AI_MODELS_TIMEOUT, status_code=504, quiet=True
        ) from None
    except httpx.RequestError:
        raise KaloscopeException(
            ErrorCode.AI_MODELS_FETCH_FAILED, status_code=502, quiet=True
        ) from None
    except (ValueError, UnicodeError):
        raise KaloscopeException(
            ErrorCode.AI_MODELS_INVALID_RESPONSE, status_code=502, quiet=True
        ) from None
