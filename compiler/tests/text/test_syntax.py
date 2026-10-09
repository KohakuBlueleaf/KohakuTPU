"""The statement syntax every level shares: what each term parses to, the
canonical print, and where an error is reported."""

import pytest
from kohakuaccel.text.syntax import (
    Arrow,
    Assign,
    Call,
    Dims,
    Float,
    Int,
    Kw,
    Member,
    Name,
    Neg,
    NewAxis,
    Not,
    Offset,
    Slice,
    Span,
    Stmt,
    Str,
    TextError,
    Tuple,
    Typed,
    View,
    emit,
    fmt,
    parse,
)


def one(text: str) -> Stmt:
    (st,) = parse(text)
    return st


@pytest.mark.parametrize(
    "src,term",
    [
        ("x", Name("x")),
        ("reduce.max", Name("reduce.max")),
        ("42", Int(42)),
        ("0x8010_0000", Int(0x8010_0000, "hex")),
        ("16K", Int(16384, "size")),
        ("2M", Int(2 << 20, "size")),
        ("1.5", Float(1.5)),
        ("1e-05", Float(1e-05)),
        ("64x64x2", Dims((64, 64, 2))),
        ('"kohakutpu-l1"', Str("kohakutpu-l1")),
        ("-inf", Neg(Name("inf"))),
        ("!p", Not(Name("p"))),
        ("buf+0x40", Offset(Name("buf"), Int(0x40, "hex"))),
        ("0..H", Span(Int(0), Name("H"))),
        ("i..j", Span(Name("i"), Name("j"))),
        ("tiles(L, bq)", Call("tiles", (Name("L"), Name("bq")))),
        (
            "f(x: f16[N], n=2)",
            Call("f", (Typed("x", View("f16", (Name("N"),))), Kw("n", Int(2)))),
        ),
        ("q[h, i, :]", View("q", (Name("h"), Name("i"), Slice()))),
        ("m[:, *]", View("m", (Slice(), NewAxis()))),
        ("x[2 :]", View("x", (Slice(Int(2), None),))),
        ("x[: 8]", View("x", (Slice(None, Int(8)),))),
        ("x[lo : hi]", View("x", (Slice(Name("lo"), Name("hi")),))),
        ("x[]", View("x", ())),
        ("(1, 0)", Tuple((Int(1), Int(0)))),
        ("(x,)", Tuple((Name("x"),))),
        ("()", Tuple(())),
    ],
)
def test_a_term_parses_to_its_node_and_prints_back(src, term):
    st = one(f"op {src}\n")
    assert st.args == [term]
    assert fmt(term) == src


def test_digit_groups_are_read_and_printed_canonically():
    assert one("op 1_000 0x1_0000").args == [Int(1000), Int(0x10000, "hex")]
    assert fmt(Int(0x8010_0000, "hex")) == "0x8010_0000"


def test_a_statement_carries_target_args_annotation_block_and_position():
    text = "\n# a comment\nm = carry -inf : f32[bq]\nmap h in 0..H, i in tiles(L, bq)\n    store o[h] = x\n"
    a, b = parse(text)
    assert (a.target, a.op, a.args, a.annot) == (
        "m",
        "carry",
        [Neg(Name("inf"))],
        View("f32", (Name("bq"),)),
    )
    assert (a.line, a.col) == (3, 1)
    assert b.args == [
        Member(Name("h"), Span(Int(0), Name("H"))),
        Member(Name("i"), Call("tiles", (Name("L"), Name("bq")))),
    ]
    (st,) = b.body
    assert st.args == [Assign(View("o", (Name("h"),)), Name("x"))]
    assert (st.line, st.col) == (5, 5)


def test_a_name_followed_by_a_bracket_apart_is_a_name_and_a_tuple():
    assert one("unit mg0 MG (1, 0)").args == [
        Name("mg0"),
        Name("MG"),
        Tuple((Int(1), Int(0))),
    ]


def test_keywords_and_positionals_split_and_arrows_stand_alone():
    st = one("fill A0 <- 0x10 n=128 l1=4")
    assert st.positional() == [Name("A0"), Arrow("<-"), Int(16, "hex")]
    assert st.kwargs() == {"n": Int(128), "l1": Int(4)}


def test_a_newline_inside_brackets_does_not_end_the_statement():
    assert one("f x[1,\n  2]").args == [View("x", (Int(1), Int(2)))]


CANONICAL = """\
level l9

block p0
    x = add a, b              : f16[N, 64]
    longer_name = mul x, 2.0  : f32[N, 64]
    tile bq = 32
    send mg0
        fill A0 <- 0x8010_0000 n=128
"""


def test_canonical_text_is_a_fixed_point():
    assert emit(parse(CANONICAL)) == CANONICAL
    messy = CANONICAL.replace("              :", " :").replace("bq = 32", "bq=32")
    assert emit(parse(messy)) == CANONICAL


@pytest.mark.parametrize(
    "text,line,col",
    [
        ("a = add x,, y\n", 1, 11),
        ("level l1\n  bad indent\n x\n", 3, 2),
        ("op $\n", 1, 4),
        ("op (1, 2\n", 1, 8),
    ],
)
def test_an_error_names_its_line_and_column(text, line, col):
    with pytest.raises(TextError) as e:
        parse(text)
    assert (e.value.line, e.value.col) == (line, col)
