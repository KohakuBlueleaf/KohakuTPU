---
title: toyaccel
summary: The smallest accelerator that exercises the framework, with its software stack; the template a project starts from.
tags:
  - template
---

# toyaccel

`y = a*x + y` over a vector of float32. That is all it computes, and that is the
point.

KohakuTPU is the wrong thing to read first. It demonstrates a **large system**,
and someone evaluating KohakuAccel has to separate what is the framework from
what is a tensor accelerator. This goes the other way: the smallest thing that
needs every framework mechanism, and nothing else.

## Run it

    python -m toyaccel.application.run

No hardware, no bitstream, no build. `kohakuaccel.simulation.machine.SimMachine`
answers on the same registers a card does, so the same driver code runs against
either.

    (1, 0): 'SX' v1  {'type': 21336, 'name': 'SX', 'version': 1, ...}
    (2, 0): 'SX' v1  {'type': 21336, 'name': 'SX', 'version': 1, ...}
    dispatched 2 units, DONE=0, 16 commands
    64/64 elements correct

## What it needs, and what that exercises

| saxpy must | which exercises |
|---|---|
| be found on the mesh | discovery over the raw-flit mailbox: `CU_CAPS`, `CU_VERSION` |
| receive `a`, and where `x` and `y` live | instruction encoding: the 256-bit payload a unit owns |
| read and write memory | the host memory window |
| say it is done | completion signalling, credits, the `NODE_STATUS` mirror |
| run on more than one unit | dispatch-before-wait, so units overlap |

## The stack

The package is laid out by component, as every project is
(`software/README.md`):

| module | what it is |
|---|---|
| `toyaccel/compiler/isa.py` | the instruction, declared as a field table (`kohakuaccel.compiler.isa`) |
| `toyaccel/driver/isa.py` | the same instruction, hand-packed: encode, decode, the flit that carries it |
| `toyaccel/driver/machine.py` | where the units are, and how work splits across them |
| `toyaccel/driver/unit.py` | the unit type registered with the framework |
| `toyaccel/simulation/unit.py` | a model that stands in for the datapath |
| `toyaccel/application/run.py` | discover, upload, dispatch, read back, check |

## What it proves

`kohakuaccel` must not import any project, and a project imports no other
project. `software/tests/test_imports.py` checks both over every module,
toyaccel's included.

## The RTL side

There is none. `SaxpyUnit` is a Python model; the hardware it stands for would
be the compute-unit template with a multiply-add in the datapath hole. The
software stack does not depend on that existing: a driver can be written and
tested before there is anything to run it on.
