"""The firmware's memset, memcpy, memmove, memcmp and strlen against Python's."""

import random

import native
import pytest

SIZE = 256
MUTANTS = {
    "move_forward": ("if (d < s) {", "if (1) {"),
    "cmp_stops_early": ("for (; n; --n, ++x, ++y)", "for (; n > 1; --n, ++x, ++y)"),
    "set_short": ("*d++ = (unsigned char)c;", "*d++ = (unsigned char)c + (n == 3);"),
}


def fnv(buf):
    h = 0xCBF29CE484222325
    for b in buf:
        h = ((h ^ b) * 0x100000001B3) & ((1 << 64) - 1)
    return h


def model(op, a, b, n):
    buf = bytearray((i * 7 + 3) & 255 for i in range(SIZE))
    r = 0
    if op == "s":
        buf[a : a + n] = bytes([b]) * n
    elif op in "cm":
        buf[a : a + n] = bytes(buf[b : b + n])
    elif op == "x":
        for x, y in zip(buf[a : a + n], buf[b : b + n]):
            if x != y:
                r = x - y
                break
    else:
        buf[a + n] = 0
        r = bytes(buf[a:]).index(0)
    return f"{fnv(buf)} {r}"


def cases():
    rng = random.Random(1)
    out = []
    for _ in range(4000):
        n = rng.randrange(0, 128)
        a, b = rng.randrange(0, 128), rng.randrange(0, 128)
        op = rng.choice("smxl")
        if op == "s":
            b = rng.randrange(256)
        out.append((op, a, b, n))
    # memcpy only on disjoint spans: an overlap is memmove's.
    out += [("c", 0, 128, n) for n in (0, 1, 31, 32, 33, 127)]
    return out


def wrong(exe):
    all_cases = cases()
    got = native.run(exe, [" ".join(str(x) for x in c) for c in all_cases])
    return [(c, g) for c, g in zip(all_cases, got, strict=True) if g != model(*c)]


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    return tmp_path_factory.mktemp("string")


def test_every_routine_is_as_python_says(out):
    assert wrong(native.build(out, "string_cases", "lib/string/string.c")) == []


@pytest.mark.parametrize("name", MUTANTS)
def test_a_mutant_is_told(out, name):
    assert wrong(
        native.build(out, "string_cases", "lib/string/string.c", MUTANTS[name])
    )
