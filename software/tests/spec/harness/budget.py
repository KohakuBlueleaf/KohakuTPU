"""Resumable spec runs in pieces of bounded wall time (a pytest plugin, off
unless ``KTPU_SPEC_LEDGER`` is set).

``KTPU_SPEC_LEDGER=<dir>``: each test's result is appended to
``<dir>/<piece>-<worker>.jsonl`` (a ``start`` line before it runs); a test
an earlier piece holds is deselected, so rerunning resumes. A start without
a result (the process was killed) reads as ``killed`` and is not rerun.
``KTPU_SPEC_BUDGET=<seconds>``: a process starts no test after this long.
``KTPU_SPEC_TIER=<n>``: only the clause and mutant tests of tier-n vectors.
``KTPU_SPEC_LAYERS=<layer,...>``: only the vectors under those layers
(``dbg,l0,tg``: the first directory of the vector's id).
``KTPU_SPEC_RERUN=<outcome,...>``: those results are run again (the latest
piece's result counts).

A result: ``id``, ``outcome`` (passed, failed, skipped, over budget,
xfailed, xpassed, killed), ``seconds``, ``message`` (the failure's line), ``detail`` (a failure's
report), ``compiles``
(``{"rust"}``: compilations through `_rs`), ``properties`` (the test's recorded properties: a
mutant's ``broken_by``).
"""

import json
import os
import pathlib
import time

import pytest

from kohakutpu.stack import rs

LEDGER_ENV = "KTPU_SPEC_LEDGER"
BUDGET_ENV = "KTPU_SPEC_BUDGET"
TIER_ENV = "KTPU_SPEC_TIER"
LAYERS_ENV = "KTPU_SPEC_LAYERS"
#: Outcomes run again although an earlier piece holds them (``failed,killed``).
RERUN_ENV = "KTPU_SPEC_RERUN"
#: The piece a process writes; the controller names it, its workers inherit it.
PIECE_ENV = "KTPU_SPEC_PIECE"
#: Which compiler each L2 -> L1 compilation went to: ``(module, attr, key)``.
COMPILERS = ((rs, "compile", "rust"),)
#: A failure's report kept in its result (``detail``): its last characters.
DETAIL = 8000

_state = {"start": time.monotonic(), "file": None, "compiles": {}, "reports": {}}


def _counted(fn, key):
    def wrapped(*a, **k):
        _state["compiles"][key] = _state["compiles"].get(key, 0) + 1
        return fn(*a, **k)

    return wrapped


def _ledger() -> pathlib.Path | None:
    where = os.environ.get(LEDGER_ENV)
    return pathlib.Path(where) if where else None


def read(where: pathlib.Path, skip_piece: str | None = None) -> dict:
    """``{id: result}`` over every piece's file in `where` (but
    `skip_piece`'s); a start without a result is ``killed``."""
    got: dict = {}
    for p in sorted(where.glob("*.jsonl")):
        if skip_piece and p.name.startswith(f"{skip_piece}-"):
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("start"):
                got.setdefault(r["id"], {"id": r["id"], "outcome": "killed"})
            else:
                got[r["id"]] = r
    return got


def pytest_configure(config):
    where = _ledger()
    if where is None:
        return
    os.environ.setdefault(PIECE_ENV, time.strftime("%Y%m%d-%H%M%S"))
    where.mkdir(parents=True, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    _state["file"] = where / f"{os.environ[PIECE_ENV]}-{worker}.jsonl"
    for mod, attr, key in COMPILERS:
        setattr(mod, attr, _counted(getattr(mod, attr), key))


def _tier(item) -> int | None:
    params = getattr(getattr(item, "callspec", None), "params", {})
    v = params.get("v")
    if v is None or item.originalname not in ("test_clause", "test_mutant_fails"):
        return None
    return v["tier"]


def pytest_collection_modifyitems(config, items):
    where = _ledger()
    if where is None:
        return
    tier = os.environ.get(TIER_ENV)
    layers = set(filter(None, os.environ.get(LAYERS_ENV, "").split(",")))
    again = set(filter(None, os.environ.get(RERUN_ENV, "").split(",")))
    done = read(where, skip_piece=os.environ[PIECE_ENV])
    done = {i for i, r in done.items() if r["outcome"] not in again}
    keep, gone = [], []
    for it in items:
        wanted = tier is None or _tier(it) == int(tier)
        layer = it.nodeid.partition("[")[2].split("/")[0]
        wanted &= not layers or layer in layers
        (keep if wanted and it.nodeid not in done else gone).append(it)
    if gone:
        config.hook.pytest_deselected(items=gone)
        items[:] = keep


def _write(record: dict) -> None:
    with _state["file"].open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    if _state["file"] is None:
        return None
    budget = os.environ.get(BUDGET_ENV)
    if budget and time.monotonic() - _state["start"] > float(budget):
        return True  # not run: the next piece takes it
    _write({"id": item.nodeid, "start": True})
    _state["compiles"] = {}
    _state["reports"][item.nodeid] = []
    return None


def _outcome(reports) -> tuple[str, object]:
    for r in reports:
        if r.failed:
            return ("xpassed" if hasattr(r, "wasxfail") else "failed"), r
    for r in reports:
        if r.skipped and hasattr(r, "wasxfail"):
            return "xfailed", r
        if r.skipped:
            over = dict(r.user_properties).get("over_budget") is not None
            return ("over budget" if over else "skipped"), r
    return "passed", None


def _message(r) -> str:
    """The failure's one line: the xfail reason, the skip reason, or the
    exception's message."""
    if r is None:
        return ""
    if getattr(r, "wasxfail", ""):
        return str(r.wasxfail)
    if r.skipped and isinstance(r.longrepr, tuple):
        return r.longrepr[2]
    crash = getattr(r.longrepr, "reprcrash", None)
    if crash is not None:
        return crash.message.splitlines()[0] if crash.message else ""
    lines = [ln for ln in str(r.longrepr or "").splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def pytest_runtest_logreport(report):
    if _state["file"] is None or report.nodeid not in _state["reports"]:
        return
    reports = _state["reports"][report.nodeid]
    reports.append(report)
    if report.when != "teardown":
        return
    outcome, why = _outcome(reports)
    record = {
        "id": report.nodeid,
        "outcome": outcome,
        "seconds": round(sum(r.duration for r in reports), 3),
        "message": _message(why),
        "compiles": dict(_state["compiles"]),
        "properties": dict(report.user_properties),
    }
    if outcome in ("failed", "xpassed"):
        record["detail"] = str(why.longrepr)[-DETAIL:]
    _write(record)
    del _state["reports"][report.nodeid]


def summary(where: pathlib.Path) -> str:
    """Outcomes by layer and test kind, the compilations by compiler, and
    every non-pass message with its count, by layer."""
    results = read(where)
    table: dict = {}
    causes: dict = {}
    compiles = {"rust": 0, "tests_rust": 0}
    for r in results.values():
        kind, _, rest = r["id"].partition("[")
        layer = (kind.split("::")[-1].removeprefix("test_"), rest.split("/")[0])
        row = table.setdefault(layer, {})
        row[r["outcome"]] = row.get(r["outcome"], 0) + 1
        if r["outcome"] in ("failed", "xpassed", "killed"):
            key = (layer[1], r["outcome"], r.get("message", "")[:160])
            causes[key] = causes.get(key, 0) + 1
        got = r.get("compiles", {})
        compiles["rust"] += got.get("rust", 0)
        compiles["tests_rust"] += bool(got.get("rust"))
    lines = [f"{len(results)} results in {where}"]
    for (kind, layer), row in sorted(table.items()):
        lines.append(f"  {layer:5} {kind:12} {json.dumps(row, sort_keys=True)}")
    lines.append(f"compiles: {json.dumps(compiles)}")
    for (layer, outcome, msg), n in sorted(causes.items()):
        lines.append(f"  {layer:5} {outcome:8} x{n}: {msg}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary(pathlib.Path(os.environ.get(LEDGER_ENV, "."))))


__all__ = [
    "BUDGET_ENV",
    "LAYERS_ENV",
    "LEDGER_ENV",
    "PIECE_ENV",
    "RERUN_ENV",
    "TIER_ENV",
    "read",
    "summary",
]
