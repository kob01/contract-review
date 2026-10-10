"""依赖注入。"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.db.session import get_db

__all__ = ["Settings", "get_db", "get_settings"]
