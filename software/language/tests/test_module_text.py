"""A module kept as text: one fn's params come from its header line alone,
equal to what the whole module reads, and the whole text is read only when
something other than params is asked for."""

import pytest

from kohakutpu.language.text import module as M

TEXT = """target ktpu.v9 vregs=64
fn k.l2(x: f16[64, 128] @dram(rows=64), y: mx7a[4, 64] @dram(gm=1, nk=2))
    barrier
fn k.l1(x: f16[64, 128] @dram(rows=64), y: mx7a[4, 64] @dram(gm=1, nk=2), t: u64[16] @stage(node=1))
    send vc[0]
        run image()
    barrier
image image()
    vhalt
"""


def test_params_from_the_header_equal_the_read_module(monkeypatch):
    whole = M.read(TEXT)
    monkeypatch.setattr(M, "read", lambda *a, **k: pytest.fail("whole text read"))
    lazy = M.Text(TEXT)
    for level in ("l1", "l2"):
        assert lazy.params("k", level) == whole.body("k", level).params
    assert lazy.source == TEXT


def test_other_attributes_read_the_whole_text_once():
    lazy = M.Text(TEXT)
    assert lazy.body("k", "l1").stmts == M.read(TEXT).body("k", "l1").stmts
    assert lazy.target == "ktpu.v9" and lazy.vregs == 64
    assert lazy.module() is lazy.module()
    assert lazy.params("k", "l1") == M.read(TEXT).body("k", "l1").params


def test_a_missing_fn_is_named():
    with pytest.raises(KeyError, match="no fn q.l1"):
        M.Text(TEXT).params("q", "l1")
