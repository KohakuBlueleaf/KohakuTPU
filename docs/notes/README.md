---
title: Notes
summary: Design rationale and open research — decisions and their reasoning, and what is still undecided.
tags:
  - notes
  - research
---

# Notes

Two kinds of page live here, and they are the two kinds that do not fit anywhere
else in the tree.

**Rationale.** Why a decision went the way it did, when the reasoning is worth
more than the outcome and would clutter the page that states the outcome. A doc
in [arch/](../arch/README.md) or [spec/](../spec/README.md) says what is true; a
note says what the alternatives were and what ruled them out.

**Open research.** Design space that has been surveyed and not decided. A note of
this kind is written so the next person argues with a position rather than
starting from nothing, and it says plainly which parts are measured, which are
arithmetic, and which are assumption.

Both kinds are allowed to be wrong later. That is the point of separating them
from the normative tree: a note can be superseded without invalidating anything
that cites it, because nothing normative should cite one.

## What is here

| | |
|---|---|
| [cache/](cache/README.md) | Staging and caching: four candidate designs for what should sit between DRAM and the compute units, and why the answer is probably not a cache. |

## Two house rules

**Label the provenance of every number.** These pages mix measured figures,
arithmetic derived from them, and assumptions supplied by whoever was thinking out
loud. A note that does not distinguish them is worse than one with no numbers,
because the reader cannot tell which parts survive a correction. The cache notes
mark measured figures explicitly; do the same.

**Measured figures in a note are still project measurements.** Any Fmax, LUT count
or utilisation quoted here describes one accelerator on one part — for the
reference instance, `xcvu13p-fhgb2104-2L-e` — and the same rule applies as
everywhere else in the tree: they are evidence, not specification, and the
canonical copy lives with the project that produced them
([projects/kohakutpu/results.md](../projects/kohakutpu/results.md)).

**Project-specific open questions belong with the project.** A question about how
KohakuTPU should spend its own guard bits is a KohakuTPU question and stays in
[projects/kohakutpu/](../projects/kohakutpu/README.md); a question about what the
framework should provide between DRAM and a compute unit is a framework question
and belongs here, even when the numbers motivating it came from one project.
