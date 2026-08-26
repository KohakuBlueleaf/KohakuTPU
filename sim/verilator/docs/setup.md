# Setup

## Install

Verilator was on none of the three environments this machine offers. It is now
installed **in WSL only**:

```
wsl -d Ubuntu-24.04 -- sudo apt-get install -y verilator     # 5.020
```

Removing it is `sudo apt-get remove verilator`. Nothing was installed on Windows
and nothing in the repo depends on it being present.

### Why WSL and not the alternatives

| Path | Version | Verdict |
|---|---|---|
| WSL `Ubuntu-24.04` apt | **5.020** | **Chosen.** Has `--timing`, so `#N` delays and `@(posedge)` work. Reads the Windows tree at `/mnt/c`; nothing is copied. |
| MSYS2 `mingw-w64-x86_64-verilator` | 4.220 | Rejected. 4.x has no `--timing`; every bench here generates its clock with `always #N`, so delays would be dropped silently. |
| Native Windows binary | — | Not offered upstream. |

For a C++ card harness ([card-backend.md](card-backend.md)) WSL is also the
right long-term home: g++ 13.3 is there, and the harness is a Linux binary.

## Running a bench

`scripts/py/vlt.py` **imports `BENCHES` from `scripts/py/xsim.py`**. That file
stays the single source of truth for what a bench is made of; adding a source
file there reaches Verilator with no second edit. `xsim.py` is never modified.

```
python scripts/py/vlt.py fpacc
python scripts/py/vlt.py cluster_node --keep
python scripts/py/vlt.py ctrlpe_mesh --lint-only
python scripts/py/vlt.py mag_link --warn          # show the silenced warnings
```

| Flag | |
|---|---|
| `--lint-only` | elaborate and check, no C++ build. Seconds. |
| `--keep` | keep the build directory (the binary is `obj_dir/vsim`) |
| `--warn` | do not silence the bulk warning classes |
| `--native` | use a `verilator` on `PATH` instead of WSL, for a Linux checkout |
| `--build-root` | build somewhere other than `build/` |
| `-d NAME=VAL` | extra `+define+` |

`--lint-only` is the fast loop: it catches missing modules, port mismatches and
parameter errors in seconds where xsim takes ~40 s to reach the same line.

## What the runner does that is not obvious

Three things it must do to match `xsim.py`, each of which cost a debugging round
when it was missing:

1. **Shims first in the file list**, so they win module lookup before any `-I`
   directory is searched for a same-named file.
2. **`PE_DIR` through a generated `kohaku_predef.vh`**, absolute. The nine PE
   benches default it to `../../tests/pe/build`, which resolves against the run
   directory; without this they report "no cases" while the images exist. This is
   exactly what `xsim.py` does and for the same reason.
3. **`--timescale 1ns/1ps`**, matching `xelab`'s `-timescale`. Most RTL here
   carries no `timescale` of its own.

## Silenced warnings

`vlt.py` silences a fixed list (`LATCH`, `WIDTHTRUNC`, `WIDTHEXPAND`,
`PINMISSING`, `TIMESCALEMOD`, `INITIALDLY`, `UNOPTFLAT`, …) and passes
`-Wno-fatal`, because Verilator makes warnings fatal by default and this codebase
trips them in bulk.

They are **not noise**. `--warn` shows them, and some are real findings xsim has
never reported — 160 `LATCH` in `cluster_node`, and width truncations like
`wire signed [8:0] exp2_k = rr_v >>> 17;` (`vec_alu.v:440`) discarding 16 bits.
Worth a pass on their own, separately from this work.
