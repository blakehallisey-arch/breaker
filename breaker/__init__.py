"""breaker -- a circuit breaker for a coding-agent session.

Counts what the agent has done since the human's last message and interrupts it
when the work stops looking like the ask. See breaker/hook.py for the trips.
"""
from .hook import VERSION, main  # noqa: F401

__version__ = VERSION
__all__ = ["main", "VERSION"]
