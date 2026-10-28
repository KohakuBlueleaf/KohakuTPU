"""End-to-end binding uses physical translation after virtual handle reuse."""

from kohakutpu.stack.kernel import compile


def test_compiled_fill_translates_handle_after_physical_reuse(dev):
    first = dev.alloc(64)
    physical = dev.mmu.resolve(first)[1]
    dev.free(first)
    current = dev.alloc(64)
    assert dev.mmu.resolve(current)[1] == physical and current != first
    compiled = compile(
        "target ktpu.v9\nfn translated.l2(y: u8[64] @dram)\n    fill y value=0x12345678 ew=32\n",
        "translated",
        dev.machine,
    )
    try:
        dev.run(compiled, {"y": current})
        assert dev.read(current, 64) == bytes.fromhex("78563412") * 16
    finally:
        dev.free(current)
