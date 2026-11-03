"""The waste detectors on traces whose waste is known: each must find what was put
in, and nothing in a trace without it."""

import json

import traces
import waste


def launch(i, kind, reads, writes, name=None):
    """A launch reading `reads` and writing `writes`: `{addr: dtype}`."""
    typed = {**{f"r{a}": (a, d) for a, d in reads.items()}}
    typed.update({f"w{a}": (a, d) for a, d in writes.items()})
    sig = ", ".join(f"{n}: {d}[64, 32] @dram" for n, (_, d) in typed.items())
    body = "\n".join(f"    copy w{a} <- x" for a in writes)
    l2 = f"target ktpu.v9\n\nfn k.l2({sig})\n{body}\n"
    row = dict(
        name=name or f"k{i}",
        kind=kind,
        binds={n: a for n, (a, _) in typed.items()},
    )
    return traces.Launch(
        i, row, l2, dict(cycles=1000.0, floor=dict(mg=1, vc=1, read=1, write=1))
    )


def clean():
    return [
        launch(0, "quant", {100: "f16"}, {200: "mx7a"}),
        launch(1, "matmul", {200: "mx7a"}, {300: "f16"}),
        launch(2, "convert", {300: "f16"}, {400: "gt4"}),
        launch(3, "dsl", {400: "gt4"}, {500: "f16"}),
    ]


def detected(launches):
    return sorted({f["detector"] for f in waste.detect(launches)})


def test_a_clean_trace_reads_zero():
    assert detected(clean()) == []


def test_a_second_quant_of_a_held_value_is_found():
    t = clean()[:2] + [launch(2, "quant", {100: "f16"}, {600: "mx7a"})]
    assert detected(t) == ["form_held"]
    assert waste.detect(t)[0]["launch"] == 2


def test_a_round_trip_is_found():
    t = [
        launch(0, "convert", {100: "f16"}, {200: "gt4"}),
        launch(1, "convert", {200: "gt4"}, {300: "f16"}),
        launch(2, "dsl", {300: "f16", 200: "gt4"}, {400: "f16"}),
    ]
    assert "form_held" not in detected(t[:1])
    t.insert(2, launch(2, "convert", {300: "f16"}, {500: "gt4"}))
    assert "form_held" in detected(t)


def test_a_write_nothing_reads_is_found():
    t = [
        launch(0, "dsl", {100: "f16"}, {200: "f16"}),
        launch(1, "dsl", {100: "f16"}, {200: "f16"}),
    ]
    found = waste.detect(t)
    assert [f["detector"] for f in found] == ["written_unread"]
    assert found[0]["launch"] == 0


def test_two_computations_in_a_row_are_a_seam():
    t = [
        launch(0, "dsl", {100: "f16"}, {200: "f16"}),
        launch(1, "dsl", {200: "f16"}, {300: "f16"}),
    ]
    found = waste.detect(t)
    assert [f["detector"] for f in found] == ["launch_seam"]
    assert found[0]["cycles"] == waste.value("launch_front") + waste.value("launch_gap")


def test_every_cycle_has_one_label():
    t = clean()
    table = waste.labels(t)
    n = len(t)
    want = sum(x.cycles for x in t)
    want += n * (waste.value("launch_front") + waste.value("launch_gap"))
    assert sum(table.values()) == want


def test_chains_stand_between_computations():
    t = clean()
    assert waste.chains(t) == [
        dict(before=1, kinds=["quant"], cycles=1000.0),
        dict(before=3, kinds=["convert"], cycles=1000.0),
    ]


def test_the_step_is_the_repeat_not_what_follows():
    names = list("xx") + list("abc") * 3 + list("zzzz")
    assert traces.period(names) == (8, 3)
    assert traces.period(list("abcd")) == (0, 0)


def test_the_step_is_whole_not_a_block_inside_it():
    forward = list("p") + list("abcd") * 12 + list("q")
    names = list("xy") + forward * 2 + list("zz")
    assert traces.period(names) == (2 + len(forward), len(forward))


def test_steps_that_differ_in_a_few_kernels_are_one_step():
    first = list("uvwxyzabcdefghijklmnop")
    second = list(first)
    second[3], second[17] = "1", "2"
    names = first + second + list("rs")
    assert traces.period(names) == (len(first), len(first))


def test_more_repeats_than_asked_steps_are_blocks_of_one_step():
    cold = list("xy") + list("abcdef") * 4
    warm = list("pq") * 12
    names = cold + warm + list("z")
    assert traces.period(names) == (len(cold) + 22, 2)
    assert traces.period(names, 2) == (len(cold), 24)


def test_a_repeat_far_from_the_end_is_not_the_step():
    names = list("uvwxyz") * 2 + list("abcdefghijklmnop")
    assert traces.period(names) == (0, 0)


def test_the_steps_asked_read_from_a_runs_arguments():
    assert traces.planned(["--inputs", "10", "--board", "b", "--inputs", "2"]) == 2
    assert traces.planned(["--steps", "3"]) == 3
    assert traces.planned(["--board", "b"]) is None


def test_a_run_of_many_steps_reads_one_step():
    names = list("xy") + list("abcdef") * 9 + list("z")
    assert traces.repeats(names) == (2, 9, 6)
    assert traces.period(names) == (50, 6)


def test_a_finding_past_the_step_is_not_the_steps(tmp_path):
    (tmp_path / "kernels").mkdir()
    runs = [("a", 100, 200), ("b", 200, 300)] * 2 + [("e", 100, 600)] * 2
    timed = dict(cycles=1000.0, floor=dict(mg=1, vc=1, read=1, write=1))
    rows, table = [], []
    for name, read, write in runs:
        rows.append(dict(name=name, kind="dsl", binds=dict(r=read, w=write)))
        table.append(dict(timed, name=name))
        (tmp_path / "kernels" / f"{name}.l2").write_text(
            "fn t.l2(r: f16[4] @dram, w: f16[4] @dram)\n    copy w <- r\n", "utf-8"
        )
    text = "\n".join(json.dumps(r) for r in rows)
    (tmp_path / "launches.jsonl").write_text(text, "utf-8")
    text = "\n".join(json.dumps(r) for r in table)
    (tmp_path / "timed.m.jsonl").write_text(text, "utf-8")
    (tmp_path / "meta.json").write_text(json.dumps(dict(args=[])), "utf-8")
    report = waste.report(tmp_path, "m")
    assert report["step_launches"] == 2
    assert [f["launch"] for f in report["findings"]] == [3]


def test_a_replay_is_an_event_of_its_own_not_a_launch_gap(tmp_path):
    """A TinyJit replay between two ordinary launches is its own row: the launch
    after it keeps only its own gap, and the replay's host time is reported apart."""
    (tmp_path / "kernels").mkdir()
    (tmp_path / "kernels" / "a.l2").write_text(
        "fn t.l2(r: f16[4] @dram, w: f16[4] @dram)\n    copy w <- r\n", "utf-8"
    )
    launch = dict(name="a", kind="dsl", binds=dict(r=100, w=200), submit_ms=1.0)
    rows = [
        dict(launch, seq=0, phase="ordinary", gap_ms=2.0),
        dict(
            seq=1,
            phase="replay",
            name="replay",
            kind="replay",
            gap_ms=3.0,
            submit_ms=900.0,
        ),
        dict(launch, seq=2, phase="ordinary", gap_ms=4.0),
    ]
    text = "\n".join(json.dumps(r) for r in rows)
    (tmp_path / "launches.jsonl").write_text(text, "utf-8")
    timed = dict(name="a", cycles=1000.0, floor=dict(mg=1, vc=1, read=1, write=1))
    (tmp_path / "timed.m.jsonl").write_text(json.dumps(timed), "utf-8")
    (tmp_path / "meta.json").write_text(json.dumps(dict(args=[])), "utf-8")
    assert [x.gap_ms for x in traces.load(tmp_path, "m")] == [2.0, 4.0]
    host = waste.report(tmp_path, "m")["host_ms"]
    assert host["replays"] == 1
    assert (host["replay_between"], host["replay_publish"]) == (3.0, 900.0)


def test_writes_read_the_kernel_text():
    l2 = "fn t.l2(a: f16[4] @dram, b: f16[4] @dram)\n    quant b <- a\n"
    assert traces.writes(l2, ("a", "b")) == {"b"}
    assert traces.params(l2)["a"].bytes == 8
    l2 = "fn t.l2(a: f16[4] @dram, b: f16[4] @dram)\n    drain acc -> b[0 : +4]\n"
    assert traces.writes(l2, ("a", "b")) == {"b"}
    for op in ("store", "scatter", "gather"):
        l2 = f"fn t.l2(a: f16[4] @dram, b: f16[4] @dram)\n    {op} b[0 : +4] <- a\n"
        assert traces.writes(l2, ("a", "b")) == {"b"}
    l2 = "fn t.l2(a: f16[4] @dram)\n    h1 = fill a[0 : +4]  : f16[4] @vc\n"
    assert traces.writes(l2, ("a",)) == set()
