"""Manage AI connections without exposing their stored credentials."""

import asyncio
from typing import Any

import httpx
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from app.core.ai.completion import complete
from app.core.ai.discovery import fetch_models
from app.core.config import KaloscopeConfig
from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.network import NetworkTransport
from app.models.ai import (
    AICompletionConfig,
    AIModelSelection,
    AIProvider,
    AIProviderConnection,
    AIProviderPreset,
    AIProviderUpsert,
)
from app.models.user import UserInfo, UserRole
from app.services.base import BaseService
from app.utils.crypto import fernet_decrypt, fernet_encrypt


class AIProviderService(BaseService[AIProvider], model=AIProvider):
    """Persist administrator-managed provider settings and encrypted credentials."""

    @classmethod
    async def list_providers(cls, user: UserInfo) -> list[dict[str, Any]]:
        """List saved provider and model settings without revealing credentials.

        Args:
            user: Authenticated account requesting the provider settings.

        Returns:
            Configured providers, including disabled ones and `has_api_key` flags.

        Raises:
            ForbiddenException: If the account is not an administrator.
        """
        if user.role != UserRole.ADMIN:
            raise ForbiddenException
        return await cls.dump_list(AIProvider.all())

    @classmethod
    async def resolve_model(cls, selection: AIModelSelection) -> AIProvider:
        """Resolve the current configuration before publishing or calling a model.

        Args:
            selection: Stable provider ID and explicitly configured model ID.

        Returns:
            Current enabled provider with the selected model still configured.

        Raises:
            KaloscopeException: If the provider is deleted, disabled, lacks a
                required key, or no longer contains the selected model.
        """
        provider = await AIProvider.get_or_none(id=selection.provider_id)
        if provider is None:
            raise KaloscopeException(ErrorCode.AI_PROVIDER_MISSING, status_code=409)
        if not provider.enabled:
            raise KaloscopeException(ErrorCode.AI_PROVIDER_DISABLED, status_code=409)
        if selection.model_id not in provider.model_ids:
            raise KaloscopeException(ErrorCode.AI_MODEL_MISSING, status_code=409)
        if (
            provider.preset
            not in {None, AIProviderPreset.OLLAMA, AIProviderPreset.LMSTUDIO}
            and not provider.api_key
        ):
            raise KaloscopeException(ErrorCode.AI_KEY_UNAVAILABLE, status_code=409)
        return provider

    @classmethod
    async def complete(cls, config: AICompletionConfig) -> dict:
        """Execute a model call using current settings and an isolated credential.

        Args:
            config: Node settings with rendered prompts and validated model selection.

        Returns:
            Completion text, validated JSON data, model ID, and token usage.

        Raises:
            KaloscopeException: If configuration, credential decryption, model
                execution, or output validation fails.
        """
        provider = await cls.resolve_model(config.model)
        secret = None
        if provider.api_key:
            try:
                secret = await asyncio.to_thread(
                    fernet_decrypt,
                    provider.api_key,
                    KaloscopeConfig.get().secret_key_path,
                )
            except (RuntimeError, OSError):
                raise KaloscopeException(
                    ErrorCode.AI_KEY_UNAVAILABLE, status_code=409
                ) from None
        async with httpx.AsyncClient(transport=NetworkTransport(http2=True)) as client:
            return await complete(
                client, provider.base_url, provider.preset, secret, config
            )

    @classmethod
    async def discover_models(cls, obj: AIProviderConnection) -> list[str]:
        """Discover models from draft settings without modifying the saved provider.

        Args:
            obj: Draft endpoint and optional key; omitted keys reuse saved
                credentials only when the endpoint remains unchanged.

        Returns:
            Available model IDs, without credentials or upstream metadata.

        Raises:
            NotFoundException: If the referenced provider does not exist.
            BadRequestException: If a new endpoint would receive the saved key.
            KaloscopeException: If the upstream catalog cannot be fetched.
            RuntimeError: If the encryption key or ciphertext is invalid.
            OSError: If the encryption key cannot be read.
        """
        provider = await cls.get(obj.id) if obj.id is not None else None
        secret = obj.api_key.get_secret_value() if obj.api_key is not None else None
        if obj.api_key is None and provider is not None and provider.api_key:
            if provider.base_url != obj.base_url:
                raise BadRequestException
            secret = await asyncio.to_thread(
                fernet_decrypt, provider.api_key, KaloscopeConfig.get().secret_key_path
            )
        # isolate credentials from the media client's persistent cache and cookies
        async with httpx.AsyncClient(transport=NetworkTransport(http2=True)) as client:
            return await fetch_models(client, obj.base_url, obj.preset, secret)

    @classmethod
    async def get(cls, id: int) -> AIProvider:
        """Find a configured provider by its stable identifier.

        Args:
            id: Provider identifier.

        Returns:
            Persisted provider configuration.

        Raises:
            NotFoundException: If no matching provider exists.
        """
        provider = await AIProvider.get_or_none(id=id)
        if provider is None:
            raise NotFoundException
        return provider

    @classmethod
    async def upsert(cls, obj: AIProviderUpsert) -> AIProvider:
        """Save connection settings and apply explicit credential changes atomically.

        Args:
            obj: Complete settings; omitted or `null` API keys retain the saved value,
                and an empty API key removes it.

        Returns:
            Saved provider, whose public serialization excludes its ciphertext.

        Raises:
            NotFoundException: If an update references a missing provider.
            BadRequestException: If a changed endpoint would reuse a stored key.
            KaloscopeException: If the configuration name is already in use.
            RuntimeError: If the workspace encryption key is missing or invalid.
            OSError: If the encryption key cannot be read or written.
        """
        try:
            async with in_transaction() as connection:
                provider = None
                if obj.id is not None:
                    provider = (
                        await AIProvider.filter(id=obj.id)
                        .using_db(connection)
                        .select_for_update()
                        .first()
                    )
                    if provider is None:
                        raise NotFoundException
                if (
                    provider is not None
                    and provider.api_key
                    and provider.base_url != obj.base_url
                    and obj.api_key is None
                ):
                    raise BadRequestException

                data = obj.model_dump(exclude={"id", "api_key"})
                if obj.api_key is not None:
                    secret = obj.api_key.get_secret_value()
                    data["api_key"] = None
                    if secret:
                        has_credentials = (
                            await AIProvider.filter(api_key__isnull=False)
                            .using_db(connection)
                            .exists()
                        )
                        data["api_key"] = await asyncio.to_thread(
                            fernet_encrypt,
                            secret,
                            KaloscopeConfig.get().secret_key_path,
                            create_key=not has_credentials,
                        )
                if provider is None:
                    return await AIProvider.create(using_db=connection, **data)
                provider.update_from_dict(data)
                await provider.save(using_db=connection)
                return provider
        except IntegrityError:
            raise KaloscopeException(
                ErrorCode.NAME_ALREADY_EXISTS, status_code=409
            ) from None

    @classmethod
    async def delete(cls, id: int) -> None:
        """Delete a configured provider and its encrypted credential.

        Args:
            id: Provider identifier; a missing record is treated as already deleted.
        """
        await AIProvider.filter(id=id).delete()
