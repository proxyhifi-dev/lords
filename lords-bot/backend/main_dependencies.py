from __future__ import annotations

from config import settings
from core.cache import SnapshotStore, TTLCache
from services.dashboard_service import DashboardService
from services.funds_service import FundsService
from services.profile_service import ProfileService

shared_cache = TTLCache()
snapshot_store = SnapshotStore(settings.cache_snapshot_file)
profile_service = ProfileService(shared_cache)
funds_service = FundsService(shared_cache)
dashboard_service = DashboardService(snapshot_store)
