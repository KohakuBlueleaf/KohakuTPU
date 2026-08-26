---
title: Notes
summary: Design rationale and open research. Not normative, not finished, and never a description of shipped hardware unless a page says so in its first line.
tags:
  - notes
  - research
---

# Notes

## A note is not a specification

**Nothing in this directory is normative.** A page here may be a proposal that
was never built, a design that was built and then replaced, or a survey of a
question nobody has answered. It may be wrong. It may contradict another page in
this directory, deliberately, because two of them are alternatives.

If you want to know what the machine *does*, read [spec/](../spec/README.md) —
which is normative and only normative — or [arch/](../arch/README.md), which
describes what exists. If you want to know what to build against, read
[integrate/](../integrate/README.md). **Nothing normative cites a note**, which
is exactly what allows a note to be superseded without invalidating anything.

The value of this directory is that a reader can see which questions are open.
The risk is that a reader mistakes a note for a decision. Everything below
exists to keep the two apart.

## Every page states its status in its first line

One of four words, and it is the first thing on the page after the title:

| status | meaning |
|---|---|
| **built** | this design exists in the RTL. The page says where, and what the RTL corrects in it |
| **proposal** | this has not been built. Nothing described here exists |
| **superseded** | this was built or decided, and something else replaced it. Kept as the record |
| **open question** | a survey with no answer yet. The page says which parts are measured, which are arithmetic, and which are assumption |

A page whose first line does not say one of those four is a bug in this
directory.

## Two kinds of page live here

**Rationale.** Why a decision went the way it did, when the reasoning is worth
more than the outcome and would clutter the page that states the outcome. A page
in [arch/](../arch/README.md) or [spec/](../spec/README.md) says what is true; a
note says what the alternatives were and what ruled them out.

**Open research.** Design space that has been surveyed and not decided. A note
of this kind is written so the next person argues with a position rather than
starting from nothing.

## What is here

| | status | |
|---|---|---|
| [data-movement-problem](data-movement-problem.md) | open question | why movement, not arithmetic, sets the cost — stated abstractly, with a relative cost model and no absolute rates |
| [cache/](cache/README.md) | mixed — **the README says which** | staging and caching: five candidate designs for what sits between DRAM and the compute units. **Two are built and shipping; three have never been built.** |

## Two house rules

**Label the provenance of every number.** These pages mix measured figures,
arithmetic derived from them, and assumptions supplied by whoever was thinking
out loud. A note that does not distinguish them is worse than one with no
numbers, because the reader cannot tell which parts survive a correction. Mark
each figure MEASURED, arithmetic, ASSUMED, PROJECTED or ESTIMATE, explicitly.

**A measured figure in a note is still a project measurement.** Any frequency,
LUT count or utilisation quoted here describes **one accelerator on one part** —
for the reference instance, `xcvu13p-fhgb2104-2L-e` — and the same rule applies
as everywhere else in the tree: it is evidence, not specification, and the
canonical copy lives with the project that produced it.

**Project-specific open questions belong with the project.** A question about
how one accelerator should spend its own guard bits stays under
[projects/](../projects/README.md); a question about what the framework should
provide between DRAM and a compute unit is a framework question and belongs
here, even when the numbers motivating it came from one project.
