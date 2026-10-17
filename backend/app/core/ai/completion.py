"""Call Chat Completions and validate outputs before exposing them to workflows."""

import asyncio
from typing import Any

import httpx
import orjson
from jsonschema import Draft202012Validator, SchemaError, ValidationError

from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.ai import AICompletionConfig, AIProviderPreset


def output_validator(config: AICompletionConfig) -> Draft202012Validator | None:
    """Validate an inline output schema without resolving external resources.

    Args:
        config: Node settings; text output does not use the schema.

    Returns:
        A Draft 2020-12 validator, or `None` for text output.

    Raises:
        KaloscopeException: If the schema is invalid, is not an object schema,
            uses references, or exceeds the supported nesting depth.
    """
    if config.response_format == "text":
        return None
    try:
        schema = orjson.loads(config.output_schema)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError
        pending = [(schema, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 64:
                raise ValueError
            if isinstance(item, dict):
                if any(key in item for key in ("$ref", "$dynamicRef", "$recursiveRef")):
                    raise ValueError
                if (
                    "$schema" in item
                    and item["$schema"] != Draft202012Validator.META_SCHEMA["$id"]
                ):
                    raise ValueError
                pending.extend((value, depth + 1) for value in item.values())
            elif isinstance(item, list):
                pending.extend((value, depth + 1) for value in item)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)
    except (ValueError, SchemaError, RecursionError):
        raise KaloscopeException(ErrorCode.AI_SCHEMA_INVALID, status_code=400) from None


async def complete(
    client: httpx.AsyncClient,
    base_url: str,
    preset: AIProviderPreset | None,
    api_key: str | None,
    config: AICompletionConfig,
) -> dict[str, Any]:
    """Request one completion without caching, retries, or credential redirects.

    Args:
        client: Credential-isolated client using the application's network transport.
        base_url: Saved provider API endpoint.
        preset: Provider preset used for token-limit parameter compatibility.
        api_key: Decrypted credential, or `None` for an unauthenticated service.
        config: Validated settings with already-rendered prompts.

    Returns:
        Text, validated JSON data, requested model ID, and available token counts.

    Raises:
        KaloscopeException: If the schema, connection, upstream response, or output
            is invalid. Errors exclude prompts, credentials, and upstream bodies.
    """
    validator = output_validator(config)
    system_prompt = config.system_prompt
    if validator is not None:
        system_prompt += (
            "\nReturn only a JSON object matching this JSON Schema. "
            "Do not include markdown or explanations.\n"
            + orjson.dumps(validator.schema).decode()
        )
    messages = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": config.prompt})
    payload: dict[str, Any] = {
        "model": config.model.model_id,
        "messages": messages,
        "stream": False,
    }
    if config.max_tokens:
        token_field = (
            "max_completion_tokens"
            if preset == AIProviderPreset.OPENAI
            else "max_tokens"
        )
        payload[token_field] = config.max_tokens
    if validator is not None and config.native_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "workflow_output",
                "strict": True,
                "schema": validator.schema,
            },
        }
    elif validator is not None:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Accept": "application/json", "Cache-Control": "no-cache, no-store"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with asyncio.timeout(config.timeout):
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=config.timeout,
                follow_redirects=False,
            ) as response:
                if not response.is_success:
                    code = {
                        401: ErrorCode.AI_AUTH_FAILED,
                        403: ErrorCode.AI_AUTH_FAILED,
                        429: ErrorCode.AI_RATE_LIMITED,
                        400: ErrorCode.AI_REQUEST_REJECTED,
                        404: ErrorCode.AI_REQUEST_REJECTED,
                        405: ErrorCode.AI_REQUEST_REJECTED,
                        422: ErrorCode.AI_REQUEST_REJECTED,
                    }.get(response.status_code, ErrorCode.AI_REQUEST_FAILED)
                    raise KaloscopeException(code, status_code=502, quiet=True)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise ValueError
        result = orjson.loads(body)
        choice = result["choices"][0]
        message = choice["message"]
        if choice.get("finish_reason") == "length":
            raise KaloscopeException(ErrorCode.AI_OUTPUT_TRUNCATED, status_code=502)
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise KaloscopeException(ErrorCode.AI_OUTPUT_REFUSED, status_code=502)
        content = message.get("content")
        if (
            choice.get("finish_reason") != "stop"
            or not isinstance(content, str)
            or not content.strip()
            or message.get("tool_calls")
        ):
            raise ValueError
    except (TimeoutError, httpx.TimeoutException):
        raise KaloscopeException(ErrorCode.AI_TIMEOUT, status_code=504) from None
    except httpx.RequestError:
        raise KaloscopeException(ErrorCode.AI_REQUEST_FAILED, status_code=502) from None
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise KaloscopeException(
            ErrorCode.AI_RESPONSE_INVALID, status_code=502
        ) from None

    data = None
    if validator is not None:
        try:
            data = orjson.loads(content)
            validator.validate(data)
        except (ValueError, ValidationError, RecursionError):
            raise KaloscopeException(
                ErrorCode.AI_OUTPUT_INVALID, status_code=502
            ) from None
    usage = result.get("usage")
    return {
        "text": content,
        "json": data,
        "model": config.model.model_id,
        "usage": {
            key: value
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(usage, dict)
            and type(value := usage.get(key)) is int
            and value >= 0
        },
    }
