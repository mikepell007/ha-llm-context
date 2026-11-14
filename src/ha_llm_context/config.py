"""Settings from environment variables, with an optional git-ignored .env file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Read KEY=VALUE lines into os.environ without overriding existing values."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class HASettings:
    url: str
    token: str

    @classmethod
    def from_env(cls) -> HASettings:
        url = os.environ.get("HA_URL", "").rstrip("/")
        token = os.environ.get("HA_TOKEN", "")
        if not url or not token:
            raise SystemExit("Set HA_URL and HA_TOKEN in the environment or .env")
        return cls(url, token)


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key: str = ""

    @classmethod
    def from_env(cls) -> LLMSettings:
        base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        model = os.environ.get("LLM_MODEL", "")
        if not base_url or not model:
            raise SystemExit("Set LLM_BASE_URL and LLM_MODEL in the environment or .env")
        return cls(base_url, model, os.environ.get("LLM_API_KEY", ""))


_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_when(value: str, now: datetime | None = None) -> datetime:
    """Parse '90m', '2h', '1d' (relative to now) or an ISO time (naive = local)."""
    now = now or datetime.now().astimezone()
    m = _DURATION.match(value.strip())
    if m:
        return now - timedelta(**{_UNITS[m.group(2)]: float(m.group(1))})
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.astimezone()
