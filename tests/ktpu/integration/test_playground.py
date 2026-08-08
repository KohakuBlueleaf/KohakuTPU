"""Every example in the IR playground compiles, runs and matches.

The examples are read OUT OF THE PAGE rather than duplicated here. A showcase
whose examples have rotted is worse than none: it is the first thing anyone
runs, and a traceback there reads as "the whole stack is broken".
"""

import pathlib
import re

import pytest

from ktpu.viz.ir import compile_kernel

PAGE = pathlib.Path(__file__).resolve().parents[3] / "src" / "ktpu" / "viz"
PAGE = PAGE / "playground.html"


def _examples() -> dict[str, str]:
    """Pull `const EXAMPLES = {...}` out of the page, name -> source."""
    text = PAGE.read_text(encoding="utf-8")
    blob = re.search(r"const EXAMPLES = \{(.*?)\n\};", text, re.DOTALL)
    assert blob, "playground.html has no EXAMPLES block"
    body = blob.group(1)
    return dict(zip(re.findall(r'"([^"]+)": `', body), re.findall(r"`([^`]*)`", body)))


EXAMPLES = _examples()


def test_the_page_offers_examples():
    assert len(EXAMPLES) >= 6


@pytest.mark.parametrize("name", sorted(EXAMPLES), ids=lambda s: s.replace(" ", "_"))
def test_the_example_compiles_and_matches(name):
    out = compile_kernel(EXAMPLES[name])
    assert out["error"] is None, out["error"]
    assert "DIVERGES" not in out["check"], out["check"]
    assert "MATCHES" in out["check"]
    for level in ("graph", "sched", "prog", "cost"):
        assert out[level], f"{name} rendered no {level}"


def test_a_broken_kernel_reports_instead_of_raising():
    """The endpoint must never 500: a user typing into a textarea produces
    syntax errors constantly, and each one should land in the error panel."""
    out = compile_kernel("def kernel(x):\n    return x +\n")
    assert out["error"] and "SyntaxError" in out["error"]
    assert out["check"] == ""


def test_source_without_the_contract_says_so():
    out = compile_kernel("x = 1")
    assert "must define kernel(...) and INPUTS" in out["error"]


def test_a_reduction_wider_than_one_pass_is_a_clear_error():
    """VLMAX is 128, so a 256-wide row needs the TREE to carry a partial across
    passes. Not emitted yet, and it must say that rather than index off the end
    of a region."""
    out = compile_kernel(
        "def kernel(x, g):\n    return D.rmsnorm(x, g)\n\n"
        'INPUTS = {"x": (64, 256), "g": (256,)}'
    )
    assert "VLMAX" in out["error"]
