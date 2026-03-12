from __future__ import annotations

from datetime import datetime


def samco_expiry_format(value: str) -> str:
    # 2026-03-26 -> 26MAR2026
    return datetime.strptime(value, '%Y-%m-%d').strftime('%d%b%Y').upper()
