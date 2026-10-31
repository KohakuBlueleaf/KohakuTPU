"""Copy-elision rule controls and individually reversible performance mutants."""

from contextlib import contextmanager
from contextvars import ContextVar

RULES = (
    "self_copy",
    "identity_walk",
    "dense_repitch",
    "padded_epilogue",
    "product_rows",
    "product_flat",
    "store_hazard",
)
_DISABLED = ContextVar("launch_disabled_rule", default=None)


def allowed(name):
    if name not in RULES:
        raise ValueError(f"unknown launch rule: {name}")
    return name != _DISABLED.get()


@contextmanager
def keep_copy(name):
    """Mutation witness: restore one scheduler/runtime copy boundary."""
    if name not in RULES:
        raise ValueError(f"unknown launch rule: {name}")
    token = _DISABLED.set(name)
    try:
        yield
    finally:
        _DISABLED.reset(token)
