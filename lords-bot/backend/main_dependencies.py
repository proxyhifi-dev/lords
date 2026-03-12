from __future__ import annotations

from backend.config import settings
from backend.core.cache import SnapshotStore, TTLCache
from backend.services.dashboard_service import DashboardService
from backend.services.funds_service import FundsService
from backend.services.profile_service import ProfileService

shared_cache = TTLCache()
snapshot_store = SnapshotStore(settings.cache_snapshot_file)
profile_service = ProfileService(shared_cache)
funds_service = FundsService(shared_cache)
dashboard_service = DashboardService(snapshot_store)
