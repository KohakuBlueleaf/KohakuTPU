---
title: Device facts, and how to establish them
summary: What has to be nailed down about a part before a floorplan exists, and why two of these must be verified rather than inferred.
tags:
  - architecture
  - physical
---

# Device facts, and how to establish them

The following are properties of the part, not measurements of any accelerator.
They are given for the part the reference instance targets,
`xcvu13p-fhgb2104-2L-e`, as an example of the *kind* of fact that has to be
nailed down before a floorplan exists. The instance's own numbers — which mesh
on which region, which channel serves it, what has been placed — live with the
project, in [projects/kohakutpu/ship](../../projects/kohakutpu/ship.md).

| | |
|---|---|
| die regions | four, identical in hard-block census |
| boundaries between them | three |
| asymmetries | the two end regions have one crossing face rather than two; one region carries configuration and device identity |
| crossing registers per boundary | tens of thousands, **shared between both directions** — it is a total, not a per-direction budget |
| crossing latency | one cycle, transmit register to receive register, plus whatever pipelining the frequency demands |
| memory channels | one wired to each region — which is what makes a region-resident mesh able to reach its own memory without crossing |
| host bridge | in one region only, fixed by transceiver placement |

Two habits are worth more than any of those numbers.

**Verify the mapping; do not infer it.** Which memory channel is on which region
is a board fact set by pinout, and it very plausibly is not in the order the
numbering suggests. Establish it from independent witnesses — an I/O bank to
region query, the placed clock buffer's coordinate in an implemented design, and
the board's own pinout document — and treat agreement between three as the
evidence. A design built on the guessed mapping crosses a boundary for its own
memory and nothing announces it.

**Distinguish "this design places nothing there" from "nothing can be placed
there."** An implemented design that leaves a region empty is a property of that
design, not of the board.

## Convention

**Establish the channel-to-region map from three independent witnesses.**
*(Free.)* An I/O bank query, a placed clock buffer's coordinate in an
implemented design, and the board's own pinout. Agreement between three is the
evidence. The numbering is not the mapping, and a design built on the guess
crosses a boundary for its own memory.
