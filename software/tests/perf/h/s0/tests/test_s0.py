"""S0 on the device double: the driver runs small cases end to end (its own process:
it patches the device classes), every point replays and meets its exact witnesses,
the producer-packing pair differs by the consumer's quant; the witnesses count a
moved row and a wrong weight; the summary reads ring records into C, D, body, f
and unit busy."""

import csv
import json
import os
import pathlib
import subprocess
import sys

import numpy as np

from h.s0 import cases, check, collect, summary

BOARD = "multimesh_v9t8"
#: The ViT-B activation pack g1 ran (`x: f16[197, 768] -> mx7a[200, 768]`).
G1_ACTIVATION_PACK = (
    "t_74c2941840bd77978d91b58ae4182a115c0d0d1ef24c64bc1042f79dec2b4256"
)
NUMBERS = ("C", "D", "C_minus_D", "replays", "launches", "body", "f", "busy_MG0")


def driven(out: pathlib.Path, only: str, reps: str) -> list[dict]:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    cmd = [sys.executable, "-m", "h.s0.run", "--board", BOARD, "--device", "double"]
    cmd += ["--only", only, "--reps", reps, "--replays", "2", "--out", str(out)]
    subprocess.run(cmd, env=env, check=True, timeout=110)
    lines = (out / "cases.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def exact(p: dict) -> None:
    """Every repetition replayed twice, every witness element compared and equal."""
    assert "error" not in p, p
    assert sum(c["replayed"] for c in p["calls"]) == 2, p["calls"]
    assert p["check"]["nonfinite"] == 0
    for label, w in p["check"]["witness"].items():
        assert w["elements"] > 0 and w["unequal"] == 0, (p["case"], label, w)


def test_linear_and_row_points_replay_and_meet_their_witnesses(tmp_path):
    only = "^(proj.n192|kalt.k192|ln.r49|softmax.h12r49)$"
    points = driven(tmp_path, only, "1,2")
    assert sorted({(p["case"], p["R"]) for p in points}) == sorted(
        (c, r)
        for c in ("kalt.k192", "ln.r49", "proj.n192", "softmax.h12r49")
        for r in (1, 2)
    )
    for p in points:
        exact(p)
        kinds = [k for _, k in p["program"]]
        if p["family"] in ("proj",):
            assert kinds.count("matmul") == p["R"], p["program"]
            assert {f"onehot{r}" for r in cases.ONEHOT} <= set(p["check"]["witness"])
    found = json.loads((tmp_path / "kernels.json").read_text("utf-8"))
    pack = [k for k, v in found.items() if "x: f16[197, 768]" in v["signature"]]
    assert pack and all(found[k]["g1"] == [G1_ACTIVATION_PACK] for k in pack), pack
    rows = list(csv.DictReader((tmp_path / "summary.csv").open(encoding="utf-8")))
    assert len(rows) == len(points)


def test_attention_and_the_producer_packing_pair(tmp_path):
    points = driven(tmp_path, "^(attn.h3|s02.n768)$", "1")
    by = {(p["case"], p["variant"]): p for p in points}
    assert set(by) == {("attn.h3", ""), ("s02.n768", "packs"), ("s02.n768", "nopacks")}
    for p in points:
        exact(p)
    assert {"zero_v_head0", "copied_head"} <= set(by["attn.h3", ""]["check"]["witness"])
    found = json.loads((tmp_path / "kernels.json").read_text("utf-8"))

    def quants_and_packing(variant):
        program = by["s02.n768", variant]["program"]
        packing = [n for n, k in program if "q0:" in found[n]["signature"]]
        return sum(k == "quant" for _, k in program), packing

    (on, packing), (off, none) = map(quants_and_packing, ("packs", "nopacks"))
    assert packing and not none and off == on + 1, (on, off, packing)


def test_witnesses_count_a_moved_row_and_a_wrong_weight():
    case = cases.table()["proj.n192"]
    rep = case.rep(7, 0)
    o = np.random.default_rng(1).standard_normal((1, cases.TOKENS, 192))
    o = o.astype(np.float16)
    o[0, cases.cut_rows(cases.TOKENS)] = o[0, 0]
    for label, _, want in case.witness(o, rep)[1:]:
        o[0, int(label.removeprefix("onehot"))] = want
    clean = check.witnessed(case, [o], [rep])
    assert all(w["unequal"] == 0 for w in clean.values()), clean
    o[0, 50, 3] += 1
    o[0, 51, 0] += 1
    got = check.witnessed(case, [o], [rep])
    assert {k: w["unequal"] for k, w in got.items() if w["unequal"]} == {
        "same_rows": 1,
        "onehot51": 1,
    }


def ring(sysnode, cycles, busy, **label):
    unit = dict(x=1, y=0, type=0x4D47, busy=busy, retired=busy // 2)
    return dict(
        node=sysnode, run=0, start=0, cycles=cycles, moves=0, units=[unit], **label
    )


def test_the_summary_reads_ring_records_into_c_d_body_f_and_busy(tmp_path):
    (tmp_path / "kernels").mkdir()
    (tmp_path / "meta.json").write_text(json.dumps(dict(board=BOARD)), "utf-8")
    check_ = dict(
        card_f32=dict(absolute=dict(max=0.0)),
        card_mxfp7=dict(absolute=dict(max=0.0)),
        mxfp7_f32=dict(absolute=dict(max=0.0)),
        outside_footprint=0,
        nonfinite=0,
        witness={},
    )
    points, card, units, last = [], [], [], {}
    # Full: quant 10 + matmul 100 a replay; the shard's matmul 30, R=1, two replays;
    # an MG's busy count 7 a launch, through its 32-bit wrap.
    busy = (1 << 32) - 10
    for name, mm in (("fc2.n768", 100), ("fc2.n192", 30)):
        tag = dict(case=name, R=1, variant="")
        calls = [dict(call=c, variant="", replayed=c > 0) for c in range(3)]
        points.append(
            dict(tag, family="fc2", full="fc2.n768", share=1.0, shape=[197, 3072, 768])
            | dict(body=["matmul"], calls=calls, ring=dict(joined=True), check=check_)
        )
        for c in range(3):
            card.append(
                dict(tag, call=c, name="replay", kind="replay", cycles=200, steps=150)
            )
            for log, kind, cycles in ((0, "quant", 10), (1, "matmul", mm)):
                busy = (busy + 7) % (1 << 32)
                row = ring(
                    0, cycles, busy, **tag, call=c, name=kind, kind=kind, log=log
                )
                units.append(collect.differenced(last, row))
    for name, rows in (("cases", points), ("card", card), ("units", units)):
        text = "".join(json.dumps(r) + "\n" for r in rows)
        (tmp_path / f"{name}.jsonl").write_text(text, "utf-8")
    summary.summarise(tmp_path)
    got = {r["case"]: r for r in csv.DictReader((tmp_path / "summary.csv").open())}
    shard = {k: float(v) for k, v in got["fc2.n192"].items() if k in NUMBERS}
    assert shard == dict(
        C=200, D=50, C_minus_D=150, replays=2, launches=2, body=30, f=0.3, busy_MG0=14
    )
    lines = list(csv.DictReader((tmp_path / "launches.csv").open()))
    assert [(r["case"], r["pos"], float(r["cycles"])) for r in lines] == [
        ("fc2.n768", "0", 10),
        ("fc2.n768", "1", 100),
        ("fc2.n192", "0", 10),
        ("fc2.n192", "1", 30),
    ]
