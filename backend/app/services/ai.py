"""Manage AI connections without exposing their stored credentials."""

import asyncio

from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from app.core.config import KaloscopeConfig
from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    KaloscopeException,
    NotFoundException,
)
from app.models.ai import AIProvider, AIProviderUpsert
from app.services.base import BaseService
from app.utils.crypto import fernet_encrypt


class AIProviderService(BaseService[AIProvider], model=AIProvider):
    """Persist administrator-managed provider settings and encrypted credentials."""

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
