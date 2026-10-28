"""Output-slot retirement: pack waits for the old drain, not the other slot."""

from application.tests.runtime.test_pipe_batch import model  # noqa: F401

from kohakutpu.stack import kernel as K


def program(slack, wait):
    other = "    vdrain a1 <- l1[256]\n" if slack else ""
    edge = f"    vsync pack on=drain slack={slack}\n" if wait else ""
    return (
        "target ktpu.v9\nimage reuse(y, z)\n"
        "    desc a0 = y walk=(32,128)\n"
        "    desc a1 = z walk=(32,128)\n"
        "    vmov v0, k1\n    vsync fe on=math\n"
        "    vpack v0 -> l1[120] f16\n    vsync fe on=pack\n"
        "    vdrain a0 <- l1[0]\n"
        + other
        + "    vadd v0, v0, v0\n    vsync fe on=math\n"
        + edge
        + "    vpack v0 -> l1[120] f16\n    vsync fe on=pack\n    halt\n"
        "fn reuse.l1(y: f16[2048] @dram, z: f16[2048] @dram)\n"
        "    send vc[0]\n        run reuse(y,z)\n    barrier\n"
    )


def test_slot_reuse_wait_and_mutant(model):  # noqa: F811
    dev = model
    slack = 1
    y, z = dev.alloc(4096), dev.alloc(4096)
    good = K.compile(
        program(slack, True), name="reuse", machine=dev.machine, level="l1"
    )
    assert not good.verdict["refused"], good.verdict
    dev.run(good, {"y": y, "z": z})
    want = bytes.fromhex("003c") * 128
    assert dev.read(y + 120 * 32, 256) == want
    bad = K.compile(
        program(slack, False), name="reuse", machine=dev.machine, level="l1"
    )
    assert bad.verdict["refused"], bad.verdict
    assert any(f["clause"] == "L1.VER.009" for f in bad.verdict["findings"])
    # This isolated model mutant alone bypasses its already-proven static gate.
    bad.verdict = {"refused": False, "findings": []}
    dev.run(bad, {"y": y, "z": z})
    assert dev.read(y + 120 * 32, 256) != want
    dev.free(y)
    dev.free(z)
