"""Ordering boundary between deferred fusion members and immediate packages,
and the product chain (`runtime.chain`) a submit outside it flushes."""

from contextlib import contextmanager
from contextvars import ContextVar

_PENDING: ContextVar[tuple | None] = ContextVar("ktpu_deferred_launch", default=None)
#: The active product chain: generated launches go through it.
CHAIN: ContextVar = ContextVar("ktpu_product_chain", default=None)


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
    chain = CHAIN.get()
    if chain is not None and chain.dev is dev:
        chain.before_submit()


def chain_flush(dev):
    """What the product chain holds runs now."""
    chain = CHAIN.get()
    if chain is not None and chain.dev is dev:
        chain.before_submit()


def before_access(dev, addr: int, nbytes: int):
    """A host read or write of `nbytes` at `addr`: a held product it touches runs first."""
    chain = CHAIN.get()
    if chain is not None and chain.dev is dev:
        chain.access(addr, nbytes)


def held_free(dev, addr: int) -> bool:
    """Whether the free of `addr` waits for the held product (and is done
    after it runs): nothing it binds is handed out again before it runs."""
    chain = CHAIN.get()
    return chain is not None and chain.dev is dev and chain.defer_free(addr)
