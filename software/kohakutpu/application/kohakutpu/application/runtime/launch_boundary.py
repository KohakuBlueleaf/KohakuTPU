"""Ordering boundary between deferred fusion members and immediate packages."""

from contextlib import contextmanager
from contextvars import ContextVar

_PENDING = ContextVar("ktpu_deferred_launch", default=None)


@contextmanager
def pending(dev, flush):
    token = _PENDING.set((dev, flush))
    try:
        yield
    finally:
        _PENDING.reset(token)


def before_submit(dev):
    current = _PENDING.get()
    if current is not None and current[0] is dev:
        current[1]()
