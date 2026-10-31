"""The waste detectors on traces whose waste is known: each must find what was put
in, and nothing in a trace without it."""

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


def test_writes_read_the_kernel_text():
    l2 = "fn t.l2(a: f16[4] @dram, b: f16[4] @dram)\n    quant b <- a\n"
    assert traces.writes(l2, ("a", "b")) == {"b"}
    assert traces.params(l2)["a"].bytes == 8
    l2 = "fn t.l2(a: f16[4] @dram, b: f16[4] @dram)\n    drain acc -> b[0 : +4]\n"
    assert traces.writes(l2, ("a", "b")) == {"b"}
