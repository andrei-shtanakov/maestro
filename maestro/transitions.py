"""Declarative status-transition -> side-effect mapping and dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from maestro.event_log import EventType
    from maestro.notifications.base import NotificationEvent


@dataclass(frozen=True)
class StatusEffect:
    """The side effect of entering (or transitioning into) a status."""

    event: EventType | None = None
    notification: NotificationEvent | None = None
