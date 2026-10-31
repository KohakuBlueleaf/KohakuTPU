"""Copy-elision rule controls and individually reversible performance mutants."""

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
