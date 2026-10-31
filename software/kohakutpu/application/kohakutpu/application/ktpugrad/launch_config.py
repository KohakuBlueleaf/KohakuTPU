"""Launch controls and lifetime of a deferred elementwise group."""

import contextlib
import os

from kohakutpu.application.ktpugrad import launch_rules
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.launch_emit import flush_multi_stage
from kohakutpu.application.runtime import launch_boundary
from kohakutpu.application.runtime import rows as RW


def view_forms_enabled() -> bool:
    return os.environ.get("KTPU_VIEW_FORMS", "1" if R.VIEW_FORMS else "0") == "1"


def elide_contiguous_enabled() -> bool:
    return (
        os.environ.get("KTPU_ELIDE_CONTIGUOUS", "1" if R.ELIDE_CONTIGUOUS else "0")
        == "1"
    )


def elide_rule(name: str) -> bool:
    return elide_contiguous_enabled() and launch_rules.allowed(name)


def single_package_enabled() -> bool:
    return RW.single_package_enabled()


def horizontal_fusion_enabled() -> bool:
    return (
        os.environ.get("KTPU_HORIZONTAL_FUSION", "1" if R.HORIZONTAL_FUSION else "0")
        == "1"
    )


@contextlib.contextmanager
def knobs(
    *,
    view_forms: bool | None = None,
    elide_contiguous: bool | None = None,
    single_package_ast: bool | None = None,
    horizontal_fusion: bool | None = None,
    mutant: str | None = None,
):
    """Temporarily configure the four launch-reduction knobs and optional mutant."""
    prev = (
        R.VIEW_FORMS,
        R.ELIDE_CONTIGUOUS,
        RW.SINGLE_PACKAGE_AST,
        R.HORIZONTAL_FUSION,
        RW.MUTANT,
    )
    if view_forms is not None:
        R.VIEW_FORMS = bool(view_forms)
    if elide_contiguous is not None:
        R.ELIDE_CONTIGUOUS = bool(elide_contiguous)
    if single_package_ast is not None:
        RW.SINGLE_PACKAGE_AST = bool(single_package_ast)
    if horizontal_fusion is not None:
        R.HORIZONTAL_FUSION = bool(horizontal_fusion)
    if mutant is not None:
        RW.MUTANT = mutant
    try:
        yield
    finally:
        (
            R.VIEW_FORMS,
            R.ELIDE_CONTIGUOUS,
            RW.SINGLE_PACKAGE_AST,
            R.HORIZONTAL_FUSION,
            RW.MUTANT,
        ) = prev


_active_scopes: dict = {}


class _MultiStageScope:
    """Collects elementwise (`_flat`, `_rows`) and `_copy` stages across a
    fused group (or `_boxes`) and emits them as one multi-stage DSL package."""

    def __init__(self, dev, dead_addrs=(), count: int | None = None, info=None):
        self.dev = dev
        self.dead_addrs = set(dead_addrs)
        self.remaining = count
        self.info = dict(info or {})
        self.stages: list = []
        self.tmps: list = []
        self.laid_cache: dict = {}
        self.flushed = False


def _active_scope(dev) -> _MultiStageScope | None:
    return _active_scopes.get(id(dev))


@contextlib.contextmanager
def multi_stage_scope(dev, dead_addrs=(), count: int | None = None, info=None):
    scope = _MultiStageScope(dev, dead_addrs=dead_addrs, count=count, info=info)
    prev = _active_scopes.get(id(dev))
    _active_scopes[id(dev)] = scope

    def boundary():
        if scope.stages and not scope.flushed and RW.MUTANT != "early_consumer":
            # An immediate package may read a deferred writer (including a form conversion).
            flush_multi_stage(scope)
            scope.stages.clear()
            scope.laid_cache.clear()
            scope.flushed = False

    try:
        with launch_boundary.pending(dev, boundary):
            yield scope
    finally:
        if prev is None:
            _active_scopes.pop(id(dev), None)
        else:
            _active_scopes[id(dev)] = prev
        for t in scope.tmps:
            t.free()
        scope.tmps.clear()
        scope.laid_cache.clear()
