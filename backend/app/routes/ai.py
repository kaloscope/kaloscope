"""Expose administrator-only AI provider configuration endpoints."""

from sanic import Blueprint, HTTPResponse, Request, empty, json
from sanic_ext import validate

from app.core.ai.presets import get_provider_presets
from app.core.decorators import authorize
from app.models.ai import AIProviderConnection, AIProviderUpsert
from app.models.user import UserRole
from app.services.ai import AIProviderService

ai = Blueprint("ai", url_prefix="/ai")


@ai.post("/provider/models")
@authorize(role=UserRole.ADMIN)
@validate(json=AIProviderConnection)
async def discover_models(_: Request, body: AIProviderConnection) -> HTTPResponse:
    """List models using draft connection settings or a saved credential.

    Args:
        _: Authenticated administrator request.
        body: Validated connection details without requiring a saved name.

    Returns:
        Available model IDs without persisting draft settings.

    Raises:
        NotFoundException: If the saved provider no longer exists.
        BadRequestException: If a changed endpoint would reuse a saved key.
        KaloscopeException: If model discovery fails.
    """
    return json(
        await AIProviderService.discover_models(body),
        headers={"Cache-Control": "no-store"},
    )


@ai.get("/provider/presets")
@authorize(role=UserRole.ADMIN)
async def provider_presets(_: Request) -> HTTPResponse:
    """List defaults for built-in providers.

    Args:
        _: Authenticated administrator request.

    Returns:
        Preset metadata without credentials or fixed model versions.
    """
    return json(get_provider_presets())


@ai.get("/provider/list")
@authorize(role=UserRole.ADMIN)
async def list_providers(request: Request) -> HTTPResponse:
    """List saved connections without exposing their credentials.

    Args:
        request: Authenticated administrator request.

    Returns:
        Provider settings with `has_api_key` flags.
    """
    return json(await AIProviderService.list_providers(request.ctx.user))


@ai.get("/provider/<id:int>")
@authorize(role=UserRole.ADMIN)
async def get_provider(_: Request, id: int) -> HTTPResponse:
    """Read one configured connection without exposing its credential.

    Args:
        _: Authenticated administrator request.
        id: Provider identifier.

    Returns:
        Provider settings with a `has_api_key` flag.

    Raises:
        NotFoundException: If the provider does not exist.
    """
    return json(await AIProviderService.dump(await AIProviderService.get(id)))


@ai.post("/provider/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=AIProviderUpsert)
async def upsert_provider(_: Request, body: AIProviderUpsert) -> HTTPResponse:
    """Create or update a provider using explicit credential replacement semantics.

    Args:
        _: Authenticated administrator request.
        body: Validated connection settings and optional credential changes.

    Returns:
        Saved settings with only the credential's presence exposed.

    Raises:
        NotFoundException: If an update references a missing provider.
        BadRequestException: If a changed endpoint would retain a stored credential.
        KaloscopeException: If the provider name conflicts with an existing record.
    """
    return json(await AIProviderService.dump(await AIProviderService.upsert(body)))


@ai.post("/provider/<id:int>/delete")
@authorize(role=UserRole.ADMIN)
async def delete_provider(_: Request, id: int) -> HTTPResponse:
    """Remove a saved provider configuration.

    Args:
        _: Authenticated administrator request.
        id: Provider identifier; missing records are already considered deleted.

    Returns:
        Empty response after deletion.
    """
    await AIProviderService.delete(id)
    return empty()
