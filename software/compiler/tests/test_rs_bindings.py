"""The `_rs` bindings that replace the Python compiler's library calls,
against their frozen goldens (`software/rust/golden/bindings/`,
written by `bindings_dump.py` from the Python stack). This file outlives the
Python stack; each group also carries a mutant one of its goldens kills."""

import hashlib
import itertools
import json
import pathlib
import re

import numpy as np
import pytest

from kohakuaccel.driver.device import T_CU_INST, header
from kohakutpu.language import machine as MF
from kohakutpu.stack import rs

GOLDEN = pathlib.Path(__file__).resolve().parents[2] / "rust" / "golden" / "bindings"
HOST = {
    "f16": np.float16,
    "f32": np.float32,
    "u8": np.uint8,
    "u32": np.uint32,
    "i32": np.int32,
    "u64": np.uint64,
}


@pytest.fixture(scope="module")
def _rs():
    if rs._rs is None:
        pytest.skip("no _rs")
    return rs._rs


def cases(group: str) -> list:
    path = GOLDEN / f"{group}.json"
    if not path.exists():
        pytest.skip(f"no {path.name}: run bindings_dump.py --group {group}")
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def machine_json(name: str) -> str:
    return (MF.MACHINES / f"{name}.json").read_text(encoding="utf-8")


def same(got: bytes, want) -> bool:
    """`got` equals a frozen byte string: hex, or `{sha256, len}`."""
    if isinstance(want, str):
        return got.hex() == want
    return len(got) == want["len"] and hashlib.sha256(got).hexdigest() == want["sha256"]


def mutant_or_skip(name: str) -> str:
    if name not in rs.mutants():
        pytest.skip(f"this _rs build does not carry {name} (build_rs.py spec)")
    return name


# ------------------------------------------------------------- group 2
def test_card_machine_views(_rs):
    for c in cases("card"):
        text = machine_json(c["machine"])
        inp, want = c["input"], c["output"]
        got = _rs.machine_view(text, inp["nodes"], inp["units"])
        assert json.loads(json.dumps(got)) == want["view"], c["case"]
        for mesh, offset, global_addr, stage_addr in want["addresses"]:
            assert _rs.global_addr(text, offset, mesh) == global_addr
            assert _rs.stage_addr(text, offset, mesh) == stage_addr


# ------------------------------------------------------------- group 3
def host_value(d: dict, seed: int) -> np.ndarray:
    """tools/l1.py's seeded input of the buffer `d` (`_rs.buffer`'s dict)."""
    rng = np.random.default_rng(seed)
    dtype = HOST[d["host_dtype"]]
    if d["dtype"].startswith("mx7") or d["dtype"] in ("f16", "f32"):
        return rng.standard_normal(d["host_shape"]).astype(dtype)
    return rng.integers(0, 8, d["host_shape"]).astype(dtype)


def refusal_of(_rs, f, *args, **kw) -> str | None:
    """`_rs.Refused`'s message where `f(*args)` refuses, else None."""
    try:
        f(*args, **kw)
    except _rs.Refused as e:
        return str(e)
    return None


def refused(want) -> bool:
    return isinstance(want, dict) and "refused" in want


def same_refusal(want: dict, _rs, f, *args, **kw) -> list:
    """A frozen refusal against `f(*args)`: refused, for the frozen reason
    (its message matches the frozen `pattern`)."""
    got = refusal_of(_rs, f, *args, **kw)
    if got is None:
        return [f"accepted; Python refused ({want['reason']})"]
    if not re.search(want["pattern"], got):
        return [f"refused for another reason than {want['reason']}: {got}"]
    return []


def buffer_case(_rs, c: dict) -> list:
    """What `_rs` gets wrong in buffer case `c` (empty when it agrees); a
    frozen refusal wants `_rs.Refused`."""
    m, ty, want = machine_json(c["machine"]), c["input"]["type"], c["output"]
    if refused(want["buffer"]):
        return same_refusal(want["buffer"], _rs, _rs.buffer, ty, m)
    bad = []
    d = _rs.buffer(ty, m)
    d["host_shape"] = list(d["host_shape"])
    d["tile"] = list(d["tile"]) if d["tile"] is not None else None
    if d != want["buffer"]:
        bad.append(f"buffer {d} != {want['buffer']}")
    x = host_value(d, c["input"]["seed"])
    if not same(x.tobytes(), c["input"]["host"]):
        bad.append("the seeded host value differs from the frozen one")
    packed = _rs.buffer_pack(ty, x.tobytes(), m)
    if not same(packed, want["pack"]):
        bad.append("pack")
    if refused(want["unpack"]):
        bad += same_refusal(want["unpack"], _rs, _rs.buffer_unpack, ty, packed, m)
    elif not same(_rs.buffer_unpack(ty, packed, m), want["unpack"]):
        bad.append("unpack")
    return bad


def test_buffer_equals_the_python_buffer(_rs):
    got = {c["case"]: buffer_case(_rs, c) for c in cases("buffer")}
    bad = {k: v for k, v in got.items() if v}
    assert not bad, f"{len(bad)} of {len(got)}: {list(bad.items())[:5]}"


def test_buffer_mutant_is_killed(_rs):
    """`l1.body/nbytes` (every buffer 0 bytes) breaks every golden's dict."""
    name = mutant_or_skip("l1.body/nbytes")
    for c in cases("buffer"):
        if refused(c["output"]["buffer"]):
            continue
        got = _rs.buffer(c["input"]["type"], machine_json(c["machine"]), mutant=name)
        assert got["bytes"] != c["output"]["buffer"]["bytes"], c["case"]


def test_a_refusal_for_the_wrong_reason_is_killed(_rs):
    """`l1.body/wrong-reason` (an unknown dtype refused as a bad layout)
    still refuses; the frozen reason (`dtype`) kills it."""
    name = mutant_or_skip("l1.body/wrong-reason")
    dtype = [
        c
        for c in cases("buffer")
        if refused(c["output"]["buffer"]) and c["output"]["buffer"]["reason"] == "dtype"
    ]
    assert dtype, "no frozen dtype refusal"
    for c in dtype:
        m = machine_json(c["machine"])
        assert same_refusal(c["output"]["buffer"], _rs, _rs.buffer, c["case"], m) == []
        got = same_refusal(
            c["output"]["buffer"], _rs, _rs.buffer, c["case"], m, mutant=name
        )
        assert got and "another reason" in got[0], (c["case"], got)


# ------------------------------------------------------------- group 5
#: tools/core.py's flit: the driver's CU_INST header (transaction 0x40, not
#: the last flit) over the payload.
FLIT_HEADER = header(T_CU_INST, 0x40, False)


def flits_case(_rs, m: str, descs: dict, words: list, want: dict, mutant=None) -> list:
    """`vc_flits` against a frozen `{flits}` or refusal."""
    if refused(want):
        return same_refusal(want, _rs, _rs.vc_flits, descs, words, 0, m, mutant=mutant)
    flits = _rs.vc_flits(descs, words, 0, m, mutant=mutant)
    return [] if [hex(FLIT_HEADER | f) for f in flits] == want["flits"] else ["flits"]


def image_case(_rs, c: dict, mutant=None) -> list:
    """What `_rs` gets wrong in image case `c`: an image's words, descs and
    flits, or a frozen refusal of the image or of its flits."""
    m, i, want = machine_json(c["machine"]), c["input"], c["output"]
    if "text" not in i:
        descs = {(ad, f): v for ad, f, v in i["descs"]}
        return flits_case(_rs, m, descs, i["words"], want, mutant)
    args = (i["text"], i["image"], i["args"], m, i["entry"])
    if refused(want):
        return same_refusal(want, _rs, _rs.image, *args)
    words, descs = _rs.image(*args)
    bad = []
    if [hex(w) for w in words] != want["words"]:
        bad.append("words")
    if [[ad, f, hex(v)] for (ad, f), v in sorted(descs.items())] != want["descs"]:
        bad.append("descs")
    return bad + flits_case(_rs, m, descs, words, want, mutant)


def test_image_equals_the_python_encoder(_rs):
    got = {c["case"]: image_case(_rs, c) for c in cases("image")}
    bad = {k: v for k, v in got.items() if v}
    assert not bad, f"{len(bad)} of {len(got)}: {list(bad.items())[:5]}"


def test_image_mutant_is_killed(_rs):
    """`l1.img/flit-pc` (RUN one word late) breaks every image's flits."""
    name = mutant_or_skip("l1.img/flit-pc")
    for c in cases("image"):
        if "flits" in c["output"]:
            assert image_case(_rs, c, mutant=name) == ["flits"], c["case"]


# ------------------------------------------------------------- group 1
#: The core every cores case runs on.
CORE = (2, 1)


def core_state(cores) -> dict:
    """`_rs.Cores.state` in `bindings_dump.state`'s form."""
    s = cores.state(CORE)
    return {
        "slots": [list(x) for x in s["slots"]],
        "last": s["last"],
        "desc": [[list(k), list(v)] for k, v in s["desc"]],
        "mem": [list(x) for x in s["mem"]],
    }


def cores_case(_rs, c: dict, mutant=None) -> list:
    """`_rs.Cores` over case `c`'s kernels in turn: what differs from the
    frozen rewrites and states (a refused rewrite forgets, as the runtime
    does)."""
    i = c["input"]
    cores = _rs.Cores(machine_json(c["machine"]))
    bad = []
    for k, (s, want) in enumerate(zip(i["streams"], c["output"]["runs"], strict=True)):
        try:
            got = cores.rewrite(CORE, [int(w, 16) for w in s], mutant=mutant)
            if "refused" in want or [hex(w) for w in got] != want["words"]:
                bad.append(f"run {k}: words")
        except _rs.Refused as e:
            cores.forget()
            if want.get("refused") != e.reason:
                bad.append(f"run {k}: refused")
        if core_state(cores) != want["state"]:
            bad.append(f"run {k}: state")
    return bad


def test_cores_equal_the_python_residents(_rs):
    got = {c["case"]: cores_case(_rs, c) for c in cases("cores")}
    bad = {k: v for k, v in got.items() if v}
    assert not bad, f"{len(bad)} of {len(got)}: {list(bad.items())[:5]}"


def test_the_eviction_case_evicts_and_places_over_other_programs():
    """A check of the golden itself: the small core's runs reuse a base."""
    (evict,) = [c for c in cases("cores") if c["case"] == "evict"]
    bases = [tuple(s) for r in evict["output"]["runs"] for s in r["state"]["slots"]]
    assert len(set(bases)) < len(bases), "no program was ever placed at a used base"
    counts = [len(r["state"]["slots"]) for r in evict["output"]["runs"]]
    assert any(b < a for a, b in itertools.pairwise(counts)), "no slot was evicted"
    assert "refused" in evict["output"]["runs"][-1], "the RUN-less kernel was taken"


def test_cores_mutant_is_killed(_rs):
    """`l1.lower/desc-always` (a DESC word its field holds still sent) breaks
    every warm run of a kernel that writes a descriptor."""
    name = mutant_or_skip("l1.lower/desc-always")
    with_descs = [c for c in cases("cores") if c["case"] != "evict"]
    with_descs = [c for c in with_descs if c["output"]["runs"][0]["state"]["desc"]]
    assert with_descs
    for c in with_descs:
        assert "run 1: words" in cores_case(_rs, c, mutant=name), c["case"]


def test_cores_wrong_reason_is_killed(_rs):
    """`bindings.cores/reason` (the RUN-less kernel refused under the same
    clause for another reason) fails only the eviction golden's last run,
    where the frozen Python reason is compared."""
    name = mutant_or_skip("bindings.cores/reason")
    (c,) = [c for c in cases("cores") if c["case"] == "evict"]
    last = len(c["output"]["runs"]) - 1
    assert cores_case(_rs, c, mutant=name) == [f"run {last}: refused"]


def test_cores_words_are_the_machines(_rs):
    for name in ("multimesh_v9t6", "sru_v9t7"):
        d = json.loads(machine_json(name))
        assert _rs.Cores(machine_json(name)).words == d["machine"]["vc"]["imem_words"]


def test_cores_digest_snapshot_restore_resident_forget(_rs):
    """The digest follows the state exactly; a restore brings back a
    snapshot's digest; `resident` is the state's slots; `forget` empties."""
    (c,) = [c for c in cases("cores") if c["case"] == "evict"]
    cores = _rs.Cores(machine_json(c["machine"]))
    empty = cores.digest()
    assert _rs.Cores(machine_json(c["machine"])).digest() == empty
    seen = {empty}
    for s in c["input"]["streams"][:6]:
        before, snap = cores.digest(), cores.snapshot()
        cores.rewrite(CORE, [int(w, 16) for w in s])
        assert cores.resident(CORE) == [tuple(x) for x in core_state(cores)["slots"]]
        after = cores.digest()
        assert after != before, "a placement left the digest unchanged"
        twin = cores.snapshot()
        assert twin.digest() == after
        cores.restore(snap)
        assert cores.digest() == before
        cores.restore(twin)
        seen.add(after)
    assert len(seen) == 7, "two different states shared a digest"
    cores.forget()
    assert core_state(cores) == {"slots": [], "last": 0, "desc": [], "mem": []}


# ------------------------------------------------------------- group 7
def isa_cases(prefix: str) -> list:
    return [c for c in cases("isa") if c["case"].startswith(prefix)]


def sru_config_case(_rs, c: dict, mutant=None) -> bool:
    want = {k: v for k, v in c["output"].items() if k != "header"}
    got = _rs.sru_config(machine_json(c["machine"]), mutant=mutant)
    return got == want


def test_sru_config_equals_the_python_map(_rs):
    (c,) = isa_cases("sru-config")
    assert sru_config_case(_rs, c)


def sru_words_case(_rs, c: dict, mutant=None) -> bool:
    i, want = c["input"], c["output"]
    try:
        got = _rs.sru_words(
            json.dumps(i["job"]),
            i["f_lanes"],
            i["h_taps"],
            machine_json(c["machine"]),
            mutant=mutant,
        )
    except _rs.Refused as e:
        return want.get("refused") == e.reason
    return "refused" not in want and [list(w) for w in got] == want["words"]


def test_sru_words_equal_the_python_words(_rs):
    got = {c["case"]: sru_words_case(_rs, c) for c in isa_cases("sru-words")}
    bad = [k for k, ok in got.items() if not ok]
    assert not bad, f"{len(bad)} of {len(got)}: {bad[:5]}"


def mg_case(_rs, c: dict, mutant=None) -> bool:
    """Whether `_rs.mg_encode` gives case `c`'s frozen payload, or refuses
    for its frozen reason."""
    i, want, m = c["input"], c["output"], machine_json(c["machine"])
    try:
        return hex(_rs.mg_encode(i["op"], i["fields"], m, mutant=mutant)) == want.get(
            "payload"
        )
    except _rs.Refused as e:
        return want.get("refused") == e.reason


def test_mg_payloads_equal_the_python_cluster_isa(_rs):
    bad = [c["case"] for c in isa_cases("mg-") if not mg_case(_rs, c)]
    assert not bad, bad
    assert any(
        "refused" in c["output"] for c in isa_cases("mg-")
    ), "no refusal is witnessed"


def test_isa_wrong_reason_is_killed(_rs):
    """`bindings.isa/reason` (a refusal given under the same clause for
    another reason) fails exactly the frozen over-count FILL refusals."""
    name = mutant_or_skip("bindings.isa/reason")
    mg = isa_cases("mg-")
    want = [c["case"] for c in mg if "refused" in c["output"]]
    assert want
    assert [c["case"] for c in mg if not mg_case(_rs, c, mutant=name)] == want


def test_vector_words_and_payloads_equal_v2_flows(_rs):
    for c in isa_cases("v2-flow"):
        i, want, m = c["input"], c["output"], machine_json(c["machine"])
        words = _rs.vc_encode(json.dumps(i["words"]), m)
        assert [hex(w) for w in words] == want["words"], c["case"]
        descs = {(ad, f): v for ad, f, v in i["descs"]}
        got = _rs.vc_flits(descs, words, i["pc"], m)
        assert [hex(p) for p in got] == want["payloads"], c["case"]


def test_isa_mutant_is_killed(_rs):
    """`isa.sru/map` (fields moved in the SRU map) breaks the map and every
    configuration that writes a moved field."""
    name = mutant_or_skip("isa.sru/map")
    (c,) = isa_cases("sru-config")
    assert not sru_config_case(_rs, c, mutant=name)
    words = isa_cases("sru-words")
    assert all(not sru_words_case(_rs, w, mutant=name) for w in words)


# ------------------------------------------------------------- group 6
def package_cases(prefix: str) -> list:
    return [c for c in cases("package") if c["case"].startswith(prefix)]


def listed(xs: list) -> object:
    """`bindings_dump.listed`: a long list as its canonical JSON's digest."""
    if len(xs) <= 16:
        return xs
    text = json.dumps(xs, separators=(",", ":"))
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "len": len(xs)}


def read_case(_rs, c: dict) -> bool:
    """`_rs.package_read` of case `c`'s package, in its golden's form."""
    want = c["output"]
    raw = bytes.fromhex(c["input"]["package"])
    try:
        d = _rs.package_read(
            raw, machine_json(c["machine"]), bindings=want.get("bindings")
        )
    except _rs.Refused:
        return "refused" in want
    got = {
        "units": [
            [u[k] for k in ("type", "x", "y", "mesh", "credit", "fetch")]
            for u in d["units"]
        ],
        "buffers": [
            [b[k] for k in ("size", "default", "kind", "mesh")] for b in d["buffers"]
        ],
        "steps": listed([[op, fl, u, n, a] for op, fl, u, n, a in d["steps"]]),
        "relocs": listed([list(r) for r in d["relocs"]]),
        "payloads": listed([hex(w) for w in d["payloads"]]),
        "signature": d["signature"],
        "ack_reserve": d["ack_reserve"],
        "checksum": d["checksum"],
        "engines": listed(
            [[i, [list(e) for e in d["engines"][i]]] for i in sorted(d["engines"])]
        ),
        "bindings": want.get("bindings"),
        "bound": listed([hex(w) for w in d["bound"]]),
    }
    return got == want


def test_package_format_equals_the_python_codes(_rs):
    (c,) = package_cases("format")
    assert _rs.package_format(machine_json(c["machine"])) == c["output"]


def test_package_read_equals_the_python_reader(_rs):
    got = {c["case"]: read_case(_rs, c) for c in package_cases("read-")}
    bad = [k for k, ok in got.items() if not ok]
    assert not bad, f"{len(bad)} of {len(got)}: {bad[:5]}"


def build_case(_rs, c: dict, mutant=None) -> bytes:
    i = c["input"]
    return _rs.package_build(
        json.dumps(i["builder"]),
        json.dumps(i["calls"]),
        machine_json(c["machine"]),
        lower=i["lower"],
        mutant=mutant,
    )


def test_package_build_equals_the_python_builder(_rs):
    bad = [
        c["case"]
        for c in package_cases("build-")
        if not same(build_case(_rs, c), c["output"]["bytes"])
    ]
    assert not bad, bad


def test_package_stream_equals_the_python_stream(_rs):
    """The debug runner's stalled packages: the one-unit package built here,
    its steps streamed as one ENGINE step; every write count a different
    package."""
    streams = package_cases("stream-")
    assert len({json.dumps(c["output"]) for c in streams}) == len(streams)
    bad = []
    for c in streams:
        i, m = c["input"], machine_json(c["machine"])
        pkg = _rs.package_build(json.dumps(i["builder"]), json.dumps(i["calls"]), m)
        if not same(pkg, i["package"]):
            bad.append(f"{c['case']}: package")
        got = _rs.package_stream(pkg, json.dumps(i["entries"]), m)
        if not same(got, c["output"]["bytes"]):
            bad.append(f"{c['case']}: stream")
    assert not bad, bad


def test_package_mutant_is_killed(_rs):
    """`l0.build/await_plus_one` (an AWAIT one completion late) breaks every
    package that awaits a unit."""
    name = mutant_or_skip("l0.build/await_plus_one")
    awaiting = [
        c for c in package_cases("build-") if "await_" in json.dumps(c["input"])
    ]
    assert awaiting
    for c in awaiting:
        assert not same(build_case(_rs, c, mutant=name), c["output"]["bytes"]), c[
            "case"
        ]
