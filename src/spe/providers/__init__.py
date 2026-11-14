"""Provider registry: each module in this package calls `register()` at import time."""

import importlib
import pkgutil
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from spe.db import Connection
from spe.settings import Settings
from spe.sure import SureClient


class NeedsAuth(Exception):
    """Session missing or expired: the owner must sign in again from the dashboard."""


class Blocked(Exception):
    """Upstream refuses unattended access (CAPTCHA, bot wall, terms). Never retried aggressively."""


@dataclass
class Context:
    db: Session
    sure: SureClient
    settings: Settings
    state: Connection  # the connection being synced: its session, cursor, config


class Provider(Protocol):
    name: str
    label: str
    interval: timedelta

    def sync(self, ctx: Context) -> str:
        """Fetch evidence, stage Sure ops. Returns a one-line summary. May set ctx.state.status='degraded'."""
        ...


REGISTRY: dict[str, Provider] = {}


def register[P: Provider](provider: P) -> P:
    REGISTRY[provider.name] = provider
    return provider


def discover() -> dict[str, Provider]:
    for mod in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{mod.name}")
    return REGISTRY
