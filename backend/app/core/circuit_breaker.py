from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int
    cooldown_seconds: int
    state: str = "CLOSED"
    failure_count: int = 0
    opened_at: datetime | None = None

    def allow_request(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if self.opened_at and datetime.now(timezone.utc) - self.opened_at >= timedelta(seconds=self.cooldown_seconds):
                self.state = "HALF_OPEN"
                return True
            return False
        return True

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = "CLOSED"
        self.opened_at = None

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            self.opened_at = datetime.now(timezone.utc)
