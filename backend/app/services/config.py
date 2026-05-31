from tortoise.expressions import Q

from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.general import ConfigUpsert, GlobalConfig
from app.services.base import BaseService


class ConfigService(BaseService[GlobalConfig], model=GlobalConfig):
    """The service class for all global config related operations."""

    @classmethod
    async def upsert(cls, obj: ConfigUpsert) -> GlobalConfig:
        """Create or update a global config.

        Args:
            obj: The global config data.

        Raises:
            KaloscopeException: If the key already exists.

        Returns:
            The global config instance.
        """
        # check if the key already exists
        filter = ~Q(id=obj.id) if obj.id else Q()
        if await GlobalConfig.filter(filter & Q(key=obj.key)).count() > 0:
            raise KaloscopeException(ErrorCode.NAME_ALREADY_EXISTS)

        if obj.id:
            # update the global config
            await GlobalConfig.filter(id=obj.id).update(value=obj.value)
            config = await GlobalConfig.get(id=obj.id)
        else:
            # create the global config
            config = await GlobalConfig.create(
                key=obj.key,
                value=obj.value,
            )

        return config
