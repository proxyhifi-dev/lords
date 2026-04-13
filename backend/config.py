# Re-export from app config for backwards compatibility
from backend.app.core.config_loader import get_settings, Settings  # noqa
settings = get_settings()
