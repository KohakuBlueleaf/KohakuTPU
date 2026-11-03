"""Round h's acceptance checks. A run's structural verdict (PASS / FAIL, or for a
bounded diagnostic MANIFEST / INCONCLUSIVE) is separate from its precision
statistics, which carry no threshold: an inexact numerical output is UNRESOLVED
until a matching-format reference explains it. A digest indexes an outcome and
accepts nothing.

A tag word names what wrote it: four f16 lanes, each a positive normal (finite,
never POISON), lane k's exponent k + 1 over a 10-bit field: the word's offset low
and high, the epoch, and producer << 6 | region. Offsets alias past 2^20 words."""

import numpy as np

from h import ledger as L
from kohakutpu.application.tools.l1 import POISON, errors

#: The producer field of words the host wrote, and of redzone words.
HOST, GUARD = 15, 14
FIELD, EPOCHS = 0x3FF, 1 << 10
#: A redzone's bytes either side of a bound region.
GUARD_BYTES = 256
POISON_WORD = int.from_bytes(bytes([POISON]) * 8, "little")
#: Mismatched words a check details, first first; `bad` lists up to `KEPT` of them
#: as (word, got, want) so every misplaced line can be located.
SHOWN, KEPT = 8, 4096
#: A memory line's words, and odd multipliers that hash one to a u64.
LINE_WORDS = 8
LINE_HASH = np.array(
    [(0x9E3779B97F4A7C15 + 2 * k * 0x2545F4914F6CDD1D) % (1 << 64) for k in range(8)],
    np.uint64,
)


def tags(epoch: int, producer: int, region: int, offsets) -> np.ndarray:
    """The tag words of `offsets` (word indexes)."""
    o = np.asarray(offsets, np.uint64)
    fields = (
        o & np.uint64(FIELD),
        (o >> np.uint64(10)) & np.uint64(FIELD),
        np.full(o.shape, epoch % EPOCHS, np.uint64),
        np.full(o.shape, producer << 6 | region, np.uint64),
    )
    word = np.zeros(o.shape, np.uint64)
    for k, f in enumerate(fields):
        word |= (np.uint64((k + 1) << 10) | f) << np.uint64(16 * k)
    return word


def tag(epoch: int, producer: int, region: int, offset: int) -> int:
    return int(tags(epoch, producer, region, [offset])[0])


def epoch_bits(epoch: int) -> int:
    """What a CPU adds to `tag(0, ...)` to make `tag(epoch, ...)`."""
    return (epoch % EPOCHS) << 32


def decode(word: int) -> dict | None:
    """A tag word's fields; None for a word no `tags` makes."""
    lanes = [(word >> (16 * k)) & 0xFFFF for k in range(4)]
    if any(lane >> 10 != k + 1 for k, lane in enumerate(lanes)):
        return None
    f = [lane & FIELD for lane in lanes]
    return dict(
        epoch=f[2], producer=f[3] >> 6, region=f[3] & 63, offset=f[0] | f[1] << 10
    )


def tagged(epoch: int, producer: int, region: int, nbytes: int, dtype) -> np.ndarray:
    """`nbytes` of tag words viewed as `dtype` (a buffer's host form)."""
    return tags(epoch, producer, region, np.arange(nbytes // 8)).view(dtype)


def words(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, np.uint64)


def hashed(lines: np.ndarray) -> np.ndarray:
    """Each row of 8 words (a 64-byte line) hashed to one u64."""
    return np.bitwise_xor.reduce(lines * LINE_HASH, axis=1)


def misplaced(g: np.ndarray, w: np.ndarray) -> list[list]:
    """Every whole 64-byte line of `g` that differs from `w`, as runs
    ``[at, home, lines, tag]``: `at` its byte offset, `home` the byte offset where
    `w` holds the same 64 bytes (None: nowhere, or a POISON line), `lines` the run's
    length (consecutive lines one displacement apart), `tag` its first word decoded."""
    n = g.size // LINE_WORDS
    got = g[: n * LINE_WORDS].reshape(n, LINE_WORDS)
    want = w[: n * LINE_WORDS].reshape(n, LINE_WORDS)
    bad = np.flatnonzero((got != want).any(axis=1))
    if not bad.size:
        return []
    keys = hashed(want)
    order = np.argsort(keys, kind="stable")
    at = np.minimum(np.searchsorted(keys[order], hashed(got[bad])), n - 1)
    home = order[at]
    found = (want[home] == got[bad]).all(axis=1)
    found &= ~(got[bad] == np.uint64(POISON_WORD)).all(axis=1)
    disp = np.where(found, home - bad, np.iinfo(np.int64).min)
    cut = np.flatnonzero((np.diff(bad) != 1) | (np.diff(disp) != 0)) + 1
    runs = []
    for lo, hi in zip(np.r_[0, cut], np.r_[cut, bad.size], strict=True):
        line = int(bad[lo])
        runs.append(
            [
                LINE_WORDS * 8 * line,
                LINE_WORDS * 8 * int(home[lo]) if found[lo] else None,
                int(hi - lo),
                decode(int(got[line, 0])),
            ]
        )
    return runs


def exact(got: bytes, want: bytes) -> dict:
    """Every word of `got` against `want`: mismatches, words left POISON that should
    not be, words whose wanted value is POISON (unwritten cannot be told from
    written there), and every differing line with where its bytes belong
    (`misplaced`)."""
    g, w = words(got), words(want)
    bad = np.flatnonzero(g != w)
    return dict(
        lines=misplaced(g, w) if bad.size else [],
        words=int(g.size),
        mismatched=int(bad.size),
        poison_left=int(((g == POISON_WORD) & (w != POISON_WORD)).sum()),
        poison_equal=int((w == POISON_WORD).sum()),
        first=[
            dict(
                at=int(i),
                got=f"{int(g[i]):#018x}",
                want=f"{int(w[i]):#018x}",
                got_tag=decode(int(g[i])),
                want_tag=decode(int(w[i])),
            )
            for i in bad[:SHOWN]
        ],
        bad=[[int(i), int(g[i]), int(w[i])] for i in bad[:KEPT]],
    )


def numeric(got: np.ndarray, ref) -> dict:
    """A numerical output: its structural counts (non-finite where the reference
    is finite, elements still POISON) and its abs / rel max, mean and median."""
    ref = np.asarray(ref, np.float64).reshape(got.shape)
    item = got.dtype.itemsize
    left = (got.view(np.uint8).reshape(-1, item) == POISON).all(axis=1)
    return dict(
        elements=int(got.size),
        nonfinite=int((~np.isfinite(got.astype(np.float64)) & np.isfinite(ref)).sum()),
        poison_left=int(left.sum()),
        **errors(got, ref),
    )


def guard(epoch: int, region: int) -> bytes:
    """A region's two redzones: the words below it, then those above it."""
    return tags(epoch, GUARD, region, np.arange(GUARD_BYTES // 4)).tobytes()


def hit(got: bytes, want: bytes) -> list[int]:
    """Redzone words a run changed (0.. below the region, then above)."""
    return np.flatnonzero(words(got) != words(want)).tolist()


def verdict(
    outputs: dict,
    guards: dict,
    faults: dict,
    ledger: list,
    diagnostic: bool,
    expected: dict | None = None,
) -> dict:
    """A run's verdict. Structural: every exact output word, no non-finite or
    POISON-left numerical element, no redzone hit, no sysnode fault and no ledger
    mismatch. A diagnostic's output mismatch is MANIFEST, its match INCONCLUSIVE;
    its redzones, faults and ledger still FAIL it. An expected failure
    (`expected`: sysnode -> text its fault must hold) is EXPECTED when exactly
    those faults came and everything else checks, else FAIL; its faults stay in
    the reasons. Precision: EXACT, UNRESOLVED (an inexact numerical output) or
    NONE (no numerical output)."""
    expect = expected or {}
    met = {n for n, text in expect.items() if text in faults.get(n, "")}
    wrong = [
        p
        for p, o in outputs.items()
        if o.get("mismatched") or o.get("nonfinite") or o.get("poison_left")
    ]
    hard = [f"redzone {p}: {w}" for p, w in guards.items() if w]
    hard += [f"sysnode {n}: {f}" for n, f in faults.items() if n not in met]
    hard += [f"sysnode {n}: no {t}" for n, t in expect.items() if n not in met]
    hard += ledger
    reasons = [f"output {p}" for p in wrong] + hard
    if expect:
        reasons += [f"expected sysnode {n}: {faults[n]}" for n in sorted(met)]
        structural = "FAIL" if wrong or hard else "EXPECTED"
    elif diagnostic and not hard:
        structural = "MANIFEST" if wrong else "INCONCLUSIVE"
    else:
        structural = "FAIL" if reasons else "PASS"
    numerics = [o for o in outputs.values() if "abs_max" in o]
    precision = (
        "NONE"
        if not numerics
        else "EXACT" if all(o["abs_max"] == 0 for o in numerics) else "UNRESOLVED"
    )
    return dict(structural=structural, precision=precision, reasons=reasons)


def sensitivity() -> dict[str, dict]:
    """Q0's checker controls on synthetic records: the captured record and each
    mutation of it, with its verdict. Every mutation must FAIL; a rerounded
    numerical output must PASS structurally and stay UNRESOLVED."""
    epoch, n = 5, 64
    want = tags(epoch, 1, 2, np.arange(n))
    z = guard(epoch, 2)
    ring = L.Ledger(rings=((1, 0, 3, 2, 1),), slots=((0, 3),))
    deltas = L.expected(ring, 4)
    slot_words = L.settled(ring)

    def run(got=want, zone=z, rings=deltas, slots=slot_words, out=None) -> dict:
        outputs = {"y": exact(got.tobytes(), want.tobytes())} | (out or {})
        return verdict(
            outputs,
            {"y": hit(zone, z)},
            {},
            L.check(ring, rings, slots),
            False,
        )

    def changed(i: int, value) -> np.ndarray:
        got = want.copy()
        got[i] = value
        return got

    dup = want.copy()
    dup[9] = dup[8]
    canary = bytearray(z)
    canary[GUARD_BYTES + 8] ^= 1
    extra = {d: dict(c) for d, c in deltas.items()}
    extra[0]["bells_from1"] += 1
    lost = {d: dict(c) for d, c in deltas.items()}
    lost[1]["bells_sent"] -= 1
    stray = {d: dict(c) for d, c in deltas.items()}
    stray[0]["bells_from1"] -= 1
    stray[0]["bells_from2"] += 1
    left = slot_words.copy()
    left[0] += np.uint64(1)
    x = np.linspace(-1, 1, 64).astype(np.float16)
    nan, ulp = x.copy(), x.copy()
    nan[3] = np.nan
    ulp[4] = np.nextafter(ulp[4], np.float16(2))
    return {
        "captured": run(),
        "wrong_word": run(changed(7, want[7] ^ np.uint64(1))),
        "missing_output": run(np.full(n, POISON_WORD, np.uint64)),
        "wrong_epoch": run(tags(epoch - 1, 1, 2, np.arange(n))),
        "wrong_source": run(tags(epoch, 3, 2, np.arange(n))),
        "duplicate": run(dup),
        "canary": run(zone=bytes(canary)),
        "ledger_extra_ring": run(rings=extra),
        "ledger_missing_ring": run(rings=lost),
        "ledger_wrong_source": run(rings=stray),
        "slot_residue": run(slots=left),
        "numeric_nonfinite": run(out={"v": numeric(nan, x)}),
        "numeric_rerounded": run(out={"v": numeric(ulp, x)}),
    }


def insensitive(result: dict[str, dict]) -> list[str]:
    """The controls `sensitivity` got wrong (none, for a checker that works)."""
    kept = {"captured": ("PASS", "NONE"), "numeric_rerounded": ("PASS", "UNRESOLVED")}
    bad = []
    for k, v in result.items():
        got = (v["structural"], v["precision"])
        if (got != kept[k]) if k in kept else (got[0] != "FAIL"):
            bad.append(k)
    return bad
