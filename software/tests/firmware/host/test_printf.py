"""ka_printf against a model of its numbers: every base, width and pad over edge
values. The sign is part of the number, the pad goes before both: "%06d" of -12
is "000-12"."""

import re

import native
import pytest

#: kind -> (bits, signed): i int, u unsigned, l int64_t, L uint64_t
KINDS = {"i": (32, True), "u": (32, False), "l": (64, True), "L": (64, False)}
FORMATS = {
    "i": ["%d", "%5d", "%05d", "%1d"],
    "u": ["%u", "%x", "%8x", "%08x", "%012u"],
    "l": ["%ld", "%22ld", "%022ld"],
    "L": ["%lu", "%lx", "%016lx", "%24lu"],
}
MUTANTS = {
    "pad_lost": ("ka_putc(pad);", "ka_putc(pad ? ' ' : pad);"),
    "no_sign": ("buf[n++] = '-';", ""),
    "hex_digit": ("v >> 4 : v / 10", "v >> 3 : v / 10"),
}


def values(kind):
    bits, signed = KINDS[kind]
    top = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
    got = {0, 1, 7, 9, 10, 15, 16, 99, 100, 255, 256, 65535, 12345, top, top - 1}
    got |= {10**k for k in range(1, 20) if 10**k <= top}
    got |= {16**k for k in range(1, 16) if 16**k <= top}
    if signed:
        got |= {-v for v in list(got) if v} | {-top - 1}
    return sorted(got)


def model(fmt, v):
    m = re.fullmatch(r"%(0?)(\d*)(l?)([dux])", fmt)
    pad, width, conv = "0" if m[1] else " ", int(m[2] or 0), m[4]
    digits = format(abs(v), "x") if conv == "x" else str(abs(v))
    text = ("-" if v < 0 else "") + digits
    return pad * (width - len(text)) + text


CASES = [(k, f, v) for k, fs in FORMATS.items() for f in fs for v in values(k)]


def wrong(exe):
    got = native.run(exe, [f"{k} {f} {v}" for k, f, v in CASES])
    return [(c, g) for c, g in zip(CASES, got, strict=True) if g != model(c[1], c[2])]


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    return tmp_path_factory.mktemp("printf")


def test_numbers_print_as_the_model_says(out):
    assert wrong(native.build(out, "printf_cases", "lib/stdio/stdio.c")) == []


@pytest.mark.parametrize("name", MUTANTS)
def test_a_mutant_is_told(out, name):
    assert wrong(native.build(out, "printf_cases", "lib/stdio/stdio.c", MUTANTS[name]))
