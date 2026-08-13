---
title: saxpy
summary: The smallest accelerator that exercises the whole framework, with its complete software stack.
tags:
  - example
---

# saxpy

`y = a*x + y` over a vector of float32. That is all it computes, and that is the
point.

KohakuTPU is the wrong thing to read first. It demonstrates a **large system** —
MXFP7, double-pumped DSP cascades, five L1-class memories, a tiling compiler, an
SDXL model — and someone evaluating KohakuAccel has to separate what is the
framework from what is a tensor accelerator. There is far too much of the
latter.

This goes the other way: the smallest thing that needs every framework
mechanism, and nothing else.

## Run it

    cd driver
    python -m examples.saxpy.sw.run

No hardware, no bitstream, no build. `kohakuaccel.sim.SimMachine` answers on the
same registers a card does, so the same driver code runs against either.

    (1, 0): 'SX' v1  {'type': 21336, 'name': 'SX', 'version': 1, ...}
    (2, 0): 'SX' v1  {'type': 21336, 'name': 'SX', 'version': 1, ...}
    dispatched 2 units, DONE=0, 13 commands
    64/64 elements correct

## What it needs, and what that exercises

| saxpy must | which exercises |
|---|---|
| be found on the mesh | discovery over the raw-flit mailbox — `CU_CAPS`, `CU_VERSION` |
| receive `a`, and where `x` and `y` live | instruction encoding: the 256-bit payload a unit owns |
| read and write memory | the host memory window |
| say it is done | completion signalling, credits, the `NODE_STATUS` mirror |
| run on more than one unit | dispatch-before-wait, so units overlap |

It needs **none** of: a number format, a tiling compiler, a schedule, a graph
IR, more than one unit type, or more than one instruction.

## The stack

Four modules, and the length of this list is KohakuAccel's real API surface for
a new accelerator.

| file | what it is |
|---|---|
| [`sw/machine.py`](sw/machine.py) | a `Target` subclass: where the units are, one optional feature, how work splits across them |
| [`sw/isa.py`](sw/isa.py) | one instruction — encode, decode, and the flit that carries it |
| [`sw/unit.py`](sw/unit.py) | the unit type registered with the framework, and a model that stands in for the datapath |
| [`sw/run.py`](sw/run.py) | discover, upload, dispatch, read back, check |

## What it proves

`kohakuaccel` must not import any project. This example is the check: it runs
against the framework alone, and `driver/tests/test_isolation.py` fails if that
ever stops being true.

The same framework carries KohakuTPU. Compare `driver/kohakutpu/machine.py` —
a `Target` subclass with eleven capacities and five optional features — against
`sw/machine.py`'s three. Neither is privileged, and the framework knows the
difference between them only through what they declared.

## The RTL side

There is none yet. `sw/unit.py`'s `SaxpyUnit` is a Python model, and the
hardware it stands for would be the compute-unit template described in
`.plan/RESTRUCTURE-PLAN.md` §3 with a multiply-add in the datapath hole. The
software stack does not depend on that existing, which is the useful part: a
driver can be written and tested before there is anything to run it on.
