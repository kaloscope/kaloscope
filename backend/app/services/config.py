from app.models.general import ConfigUpsert, GlobalConfig
from app.services.base import BaseService
from app.utils import json


class ConfigService(BaseService[GlobalConfig], model=GlobalConfig):
    """The service class for all global config related operations."""

    @classmethod
    async def upsert(cls, obj: ConfigUpsert) -> GlobalConfig:
        """Create or update a global config.

        Args:
            obj: The global config data.

        Returns:
            The global config instance.
        """
        config, _ = await GlobalConfig.get_or_create(key=obj.key)
        await GlobalConfig.filter(id=config.id).update(value=json.dumps(obj.value))
        await config.refresh_from_db()
        return config

    @classmethod
    def dump(cls, config: GlobalConfig) -> dict:
        """Serialize a GlobalConfig ORM object to a plain dict.

        Tortoise's JSONField Pydantic model rejects scalar values
        (str, int, float, bool), so we serialize manually.

        Args:
            config: The GlobalConfig ORM object to serialize.

        Returns:
            A dict containing the serialized config data.
        """
        return {
            "id": config.id,
            "created_at": config.created_at.isoformat() if config.created_at else None,
            "updated_at": config.updated_at.isoformat() if config.updated_at else None,
            "key": config.key,
            "value": config.value,
        }
