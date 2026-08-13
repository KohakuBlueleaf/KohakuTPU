"""Every example in the playground compiles, runs and matches.

The examples are read OUT OF THE PAGE rather than duplicated here. A showcase
whose examples have rotted is worse than none: it is the first thing anyone
runs, and a traceback there reads as "the whole stack is broken".

An example whose name begins with "a refusal" is expected to FAIL, by name --
those exist to show what the compiler declines and are checked as such.
"""

import re

import pytest
from kohakutpu.viz.playground import PAGE, PANELS, compile_kernel


def _examples() -> dict:
    """Pull `const EXAMPLES = {...}` out of the page, name -> source."""
    text = PAGE.read_text(encoding="utf-8")
    blob = re.search(r"const EXAMPLES = \{(.*?)\n\};", text, re.DOTALL)
    assert blob, "playground.html has no EXAMPLES block"
    body = blob.group(1)
    names = re.findall(r'^"([^"]+)": `', body, re.MULTILINE)
    sources = re.findall(r"`((?:[^`\\]|\\.)*)`", body, re.DOTALL)
    assert len(names) == len(sources), (len(names), len(sources))
    return {n: s.replace("\\`", "`") for n, s in zip(names, sources)}


EXAMPLES = _examples()
COMPUTES = [n for n in EXAMPLES if not n.startswith("a refusal")]
REFUSES = [n for n in EXAMPLES if n.startswith("a refusal")]


def test_the_page_offers_examples():
    assert len(EXAMPLES) >= 6
    assert COMPUTES and REFUSES, "both halves of the story should be shown"


@pytest.mark.parametrize("name", sorted(COMPUTES), ids=lambda s: s.split(" ")[0])
def test_the_example_compiles_and_matches(name):
    """The page reports the distribution and passes no verdict, so the bound
    lives HERE. The median is the robust statistic: a per-element relative
    error is unbounded wherever the reference is near zero.
    """
    got = compile_kernel(EXAMPLES[name])
    assert got["error"] is None, got["error"]
    for panel in PANELS:
        assert got[panel], f"{name} rendered no {panel}"
    # 3e-2 admits a contraction through MXFP7; an elementwise pass lands 1e-4.
    assert got["stats"]["rel"]["p50"] < 3e-2, got["stats"]


@pytest.mark.parametrize("name", sorted(REFUSES), ids=lambda s: s.split(" ")[0])
def test_the_refusal_example_refuses_by_name(name):
    got = compile_kernel(EXAMPLES[name])
    assert got["error"], f"{name} was supposed to be declined"
    assert "LangError" in got["error"], got["error"]


def test_a_broken_kernel_reports_instead_of_raising():
    """A person typing into a textarea produces syntax errors constantly, and
    each one should land in the error panel rather than a traceback."""
    got = compile_kernel("def kernel_fn(x):\n    return x +\n")
    assert got["error"] and "SyntaxError" in got["error"]
    assert got["check"] == ""


def test_source_without_the_contract_says_so():
    got = compile_kernel("x = 1")
    assert "must define" in got["error"]


def test_a_kernel_with_no_reference_still_renders_every_level():
    """Grading is optional; showing the levels is not."""
    got = compile_kernel(
        "@kernel\n"
        "def kernel_fn(x=L.In(...), y=L.Out(...), *, part=8192):\n"
        "    with units(x.parts(part)) as e:\n"
        "        y[e] <<= x[e] * 2.0\n"
        "\n"
        'INPUTS = {"x": (32, 64)}\n'
    )
    assert got["error"] is None, got["error"]
    assert got["stats"] == {}
    assert "nothing was graded" in got["check"]
    for panel in ("dsl", "stages", "instructions", "cost", "mesh"):
        assert got[panel], panel
