# Caveats: everything on the driver side that has bitten us

Every entry here cost real time and produced a wrong answer, a hang, or a lost
day. They are recorded because none of them are visible from reading the code
that contains them.

Ordered by how badly they mislead, not by subsystem.

---

## 1. Wrong answers that report success

### 1.1 L1 bank bits -- fixed 2026-08-11

The full account is [`../limits.md`](../limits.md) s6.8. The summary:

The driver emitted no `abank`/`bbank`/`fbank` (flit bits 114/113/112), so every
FILL wrote L1 bank 0 and every GEMM read bank 0. When a B chunk was exactly 256
entries -- a whole bank -- chunk 1's fill destroyed chunk 0 before the first
GEMM ran, and **every K-chunk was multiplied by the last chunk of B.**

Broken iff **a chunk is 256 entries AND there is more than one chunk AND B is
resident**. All three are required, which is why it hid: one chunk has nothing
to overwrite, a 128-entry chunk fits two live buffers in one bank, and a
non-resident B is refilled before each GEMM so the overwrite is harmless.

`77x2048x64` scored **p50 6.36e-05 with a max of 1.08** and a quarter of
elements wrong. **A median spot-check passes this.** Judge a numeric change on
the tail, never the median.

### 1.2 Plan and runner disagreeing about order

`memplan` derives tensor lifetimes from **plan step order**. If a script
declares buffers in one order and the runner issues them in another, the
lifetimes are wrong, the allocator reuses an address that is still live, and the
result is a plausible number computed from an aliased buffer.

This cost a transformer block scoring 1.38e-01 instead of 1.07e-03. `place.verify()`
cannot catch it: the plan it verifies is entirely self-consistent -- it is the
*runner* that disagrees with the plan.

**Rule: declare each buffer where the runner issues it.** Guarded now by
`memplan.Writes`, which raises at the READ and names what overwrote it.

### 1.3 The op field is overloaded by node type

`FILL/GEMM/DRAIN` encode as op 1/2/3 for a cluster. `IMEM/DESC/RUN` encode as
op 1/2/3 for a vector core. **Same four bits, different meanings**, disambiguated
only by which node the flit is addressed to.

A flit delivered to the wrong node type is not rejected. It decodes as whatever
that node's table says and executes.

---

## 2. The machine the tests model is not the machine that ships

`bench.py` hardcodes `L1_A_ENTRIES = 128`, `L1_B_ENTRIES = 256`. The block
design builds meshes with **`GA = GB = 512`**, and `boards/ship_3x2.json`
records 512/512 for the card.

So `bench.current_caps()` describes a machine nobody builds, and a bank in it is
64 entries rather than 256. **No simulator run at default capacities can reach a
256-entry chunk**, which is why s6.8 was invisible to xsim in both `--ncl 2` and
`--ncl 6`.

It also changes the program structurally, not just the tile:
`b_resident = chunks * chunk_b <= l1_b` lands on opposite sides of the threshold
at the two capacity sets, so the simulator builds a *differently shaped* program,
not a smaller one.

**Plan at the board's capacities when testing anything that ships.** Pass
`caps=` to `bench.build` / `bench.tile_for` rather than relying on the default.

## 3. Two functions named `choose_tile`

- `ktpu.passes.tile.choose_tile(m, k, n, target)` -- the compiler path.
- `ktpu.hw.kernel.choose_tile(tiles, l1_a, k_blocks, l1_b, m=, k=, n=, banks=)`
  -- what `bench.tile_for` calls, and therefore what the card runs.

They take different arguments, describe different machines, and **return
different tiles for the same shape**. Reading the wrong one produced a chunk
count off by 4x and sent an entire investigation to the wrong boundary.

If you want to know what the card will do, call `bench.tile_for`.

## 4. A test that replays fills can still be blind

`test_fills_name_the_entries_the_packer_wrote` replayed every FILL into a model
of L1 and asserted the packer's claims -- and still missed s6.8 completely,
because it keyed the model on `eoff` **alone**. `bank0:0` and `bank1:0` are
different entries and were the same key, so the overwrite was invisible.

Combined with s2, the suite was structurally incapable of seeing this class.
Both are fixed; the lesson is that a test which models state must key on the
*whole* address, and must model the configuration that ships.

---

## 5. The transport

### 5.1 The JTAG-to-AXI write path can skew silently

Observed on 2026-08-11: **write data lagged the write address by exactly two
256-bit memory words** -- address word `k` paired with data word `k-2` -- while
the read path stayed exact and the control aperture stayed exact.

The JTAG-to-AXI master queues **up to 16 read and 16 write transactions**. A
hung or abandoned burst leaves data beats in that queue with no address to pair
against, and every subsequent write is shifted by that many beats.

**`reset_hw_axi` cannot clear it.** Its documented effect is limited to STATUS
properties -- `AXI_READ_BUSY`, `AXI_READ_DONE`, `AXI_WRITE_BUSY`,
`AXI_WRITE_DONE`, `BRESP`, `RRESP`. Nothing flushes the queue. In practice it
*moves* the skew without fixing it.

Reconfiguring the fabric clears it, so reprogramming is the reliable recovery.

**Preflight every session.** `jtag.verify_write_path()` writes a pattern, reads
it back, and drains until a round trip is byte-exact, raising rather than
guessing if it will not converge. `Session(preflight=True)` gates on it and
records `Session.write_shift`.

**Verification is O(1), not O(N).** The skew is a single global shift, so one
probe determines it: pad the head generously, and if the head reads back
correctly the whole burst is correct. The precondition has two halves --
**head verifies AND no transaction hung during the transfer.** A hang is the one
thing that can change the shift mid-burst, and it is separately detectable.

`_write_shifted` is exercised only against an emulator. Healthy hardware cannot
reproduce a skewed queue, so the compensation path has never run on silicon.

### 5.2 Raw AXI is the upload path

There is no special packet. `board.upload_addr()` returns a plain byte address;
bits **33 (quantise)** and **32 (blayout)** are markers `mag.v` reads off the
address because AXI carries no field for them. With those clear it is an
ordinary write.

### 5.3 XDMA: the driver will blue-screen the host

`XDMA.sys` 18.29.44.170 (Xilinx, 2017) is not robust to the endpoint changing
underneath it. Reprogramming the FPGA while the driver held the device open,
with descriptors outstanding, produced **PAGE_FAULT_IN_NONPAGED_AREA in
XDMA.sys** and took the whole machine down, destroying a running synthesis.

BAR reads returning `0xFFFFFFFF` is the warning sign: the endpoint is gone while
the driver still holds mappings to it.

**Rules.** Never reprogram while the XDMA driver has the device open -- either
disable the function first, or physically disconnect. After any reprogram treat
the endpoint as invalid until it has been re-enumerated; a fresh bitstream does
not restore a dropped endpoint by itself.

XDMA itself works: driver bound, all engines enumerated, 64-bit addressing at
1-byte granularity, and a C2H read whose data JTAG confirmed byte-identically.
Throughput has never been measured and must not be quoted.

## 6. The board profile is the least verified thing in the repo

`boards/*.json` says so itself: the address map "comes from a block design
nothing in this repo can read, so they are the least verifiable fields here.
NOT VERIFIED AGAINST HARDWARE."

Run `run_fpga.py --probe` and check the node sweep before trusting `ctrl_base`,
`mem_base` or the marker bits on a board you have not used before.

## 7. The first compute after programming is broken

Known behaviour of this board. A failed first compute is **not** evidence of a
fault -- re-run before concluding anything. This has already caused one session
to declare a working card dead.
