# Contributing

House style, the tools that enforce it, and the parts still under discussion.

## Python — settled, and enforced

`black` is the formatter and `ruff` is the linter. Both are in `check.py`'s
`fast` list, which every tier includes, over **every** directory:

```
python -m ruff check .
python -m black --check -q .
```

The scope was `compiler driver scripts` and that was the same mistake the
Verilog table made below — `tests/pe/tools` alone is 34 files of generators and
golden models that every PE suite is graded by, and `demos/` is what a reader
runs first. A gate over a subset reads as done. `scripts` is in scope because it
is not scratch either: `check.py`, `xsim.py` and `gen_mesh.py` are load-bearing.

Three rules carry a per-file exemption, each with its reason in `pyproject.toml`:

- **`BLE001`** in the three test-harness trees. A harness that reports every
  failure has to catch every failure.
- **`RUF059`** in `demos/`. `B, C, H, W = x.shape` names the layout, and
  `_, _, H, W` deletes the only thing that line says.
- **`UP031`** in `tests/pe/tools/`. 442 `%` format strings across 34 generators
  and golden models, most of them aligned table rows. Ruff's own fix rewrites
  them as `.format()`, which `UP032` then flags — two mechanical passes over
  code every PE suite is graded by, to reach a style nobody asked for. `%` is
  not a defect, and an exemption with a reason beats a rewrite without one.

**Run `black` before `ruff`'s report is meaningful.** Formatting first removes
most line-length and continuation noise, so what ruff then reports is
substance. `ruff check --fix` is safe to apply; `--unsafe-fixes` is not, and
should be read case by case.

## Documentation — three things are now checked

Prose cannot be checked. Two things inside prose can, and both had rotted
silently because nothing looked:

```
python scripts/py/docpaths.py     # every repo path a doc cites, against the tree
python scripts/py/specparams.py   # docs/spec/parameters.md, against the RTL
```

Both are in `check.py`'s `fast` list, so every tier runs them.

**`docpaths.py`** checks two things over 1,249 citations in 159 files: every
repo-rooted path a doc names, and every relative markdown link. It found **103
dangling paths and 12 dangling links**. Almost all were two renames the docs
never learned about: the old `kohakunoc` tree became `src/kohakuaccel/noc/`, and
`mas` became `sysnode`. The paths are invisible to a link checker — they are
prose, `code` spans and Vue string literals — and the links were invisible to a
path check, because `../mas/` names no top-level directory at all. Both halves
were needed. A line that
deliberately names a dead path ("the retired `src/ktpu`", "directories that no
longer exist") is skipped by a word test on that line, and a placeholder like
`src/examples/NAME/NAME_cu.v` by an all-caps segment test. Both heuristics err
toward silence, because a checker that cries wolf is a checker people delete.

**`specparams.py`** found **42 mismatches** against the RTL: parameters the
tables omitted, parameters that were not that module's, and defaults that
disagreed. The worst was **`ADDR_W` documented as 34** in five tables, with a
6-bit `addr_spare` beside it in `flit-format.md` and the mesh id at
`addr[33:32]`. Every module on the memory path declares 40, `mag_mem_port.v`
slices `[255 -: 40]` through a localparam whose comment says slicing it by
`ADDR_W` "read `addr >> 6` on a 34-bit build, silently", and `address-map.md`
had it right the whole time. **A sender that followed the spec would have placed
every request 64× too high** with nothing on the path reporting it.

Four normative pages said the same wrong thing, which is what a spec tree does
when one page is copied into the next. The lesson is not "check the specs" — it
is that a normative claim about a bit position is checkable, so check it.

Both gates were watched failing before being trusted: `docpaths.py` on a
booby-trapped snapshot, `specparams.py` against the pre-fix docs, where it
reports 42 and exits 1.

## Verilog — the rules live in `format-example2.v`

**`format-example2.v` is normative.** It is every syntactic form Verilog-2001
has, each written in the shape this tree wants, with the reason beside it — read
it the way PEP-8's examples are read. Nothing here restates it; this section is
the summary and the measurement.

It is verified, not asserted. Both modules in it parse under `xvlog -sv` and
elaborate under `xelab`, and its bench module runs to `PASS -- 2 checks` with no
watchdog, so every shape shown is one the tools actually accept.

There is no formatter in the toolchain. Verible is the tool for this and is
**not packaged for win-64** on conda-forge; `verilator` is, and is a linter
only. More to the point, a formatter would not decide most of these — F4/F5/F7
are structure and F6 is which token starts a line.

| | Rule | Was | Now | Fixed by |
|---|---|---|---|---|
| **F1** | 80 columns | — | — | hand |
| **F2** | 4 spaces, never a tab | — | — | hand |
| **F3** | a declaration is ONE line; a continuation line is not | 81 | **0** | `--fix-decls` |
| **F4** | `begin`/`end` on every `if`/`else`/`for`/`while` body | 1,779 | **0** | `--fix-blocks`, then hand |
| **F4a** | the same rule applied to an `always` body | 300 | **0** | `--fix-blocks`, then hand |
| **F5** | `case` items indented one level inside `case` | 26 | **0** | `--fix-case` |
| **F6** | multi-line expression in its own parens, operator LEADING | 110 | **0** | hand |
| **F7** | `generate` adds no indent; every `begin` in one is named | 7 | **0** | hand |
| **F8** | one item per line in a port list and an instantiation | — | — | hand |
| **F9** | comments above the code, at its indent | — | — | hand |
| **F10** | `<=` under `@(posedge)`, `=` elsewhere, never both | — | — | hand |
| **F11** | every literal wider than one bit is sized and based | — | — | hand |
| **F12** | compiler directives at column 0 | — | — | hand |

**2,303 → 0 across all 391 `.v` files** under `src/` and `tests/` — every
directory, including `tests/`, `src/examples`, `src/reference` and `src/attic`.
Run `python scripts/py/vstyle.py src tests` and the total is zero.

### The scope was the thing this table got wrong

The previous version of it read **"1,283 → 146 across 180 files"** and did not
say which 180. They were `src/kohakuaccel`, `src/kohakutpu`, `src/kohakumpe`
and `src/templates` — the live source, 187 files today. The other 204 files
were never measured, and they held **more findings than the whole reported
tree**: `tests/` alone was 1,916, of which 1,507 were F4. A table that reports
a subset and calls it the tree is worse than no table, because it reads as done.

Three counts in the old table had also moved because the CHECKER was wrong, not
the tree: `if ((SEED_UNITS != 0)` is a condition broken across lines and was
read as a missing body (11), and `always @* begin` opens its block on the
header line but the sensitivity test looks for a `)` that `@*` does not have
(2). A style count is a claim; that was the third and fourth time one in this
file was wrong, and the scope error above is the fifth.

### The evidence is the numbers, not the PASS

A reformat is supposed to change no behaviour, and a green suite does not say
that: 503 checks becoming 501 is still a PASS. So `check.py` now records what
each check printed on its `PASS`/`FAIL` lines and can compare a later run
against it:

```
python scripts/py/check.py full --counts build/counts-base.json
...                                              # reformat
python scripts/py/check.py full --counts-baseline build/counts-base.json
```

Drift fails the run and prints the was/now pair. This pass cleared it twice —
after the mechanical fixers over 148 files, and again after the hand pass —
each time **104/104 ran, 104 compared**, every number identical.

`vstyle.py --lines` prints rule, line number and source text per finding, and
`--rule F6` narrows it. The counts and the listing come from the same
predicates, so they cannot disagree; the checks return line lists and the count
is their length.

`--fix-blocks` now reaches six shapes it used to refuse, each because the
refusal was about a *wrong match* rather than about the rewrite being unsafe:

- **A header whose `)` is matched, not merely trailing.** Testing "starts with
  `if (` and ends with `)`" corrupted three files — see below — and balancing the
  paren is what makes the next-line form safe to touch at all.
- **A body that spans lines**, a wrapped `$display` being the common one. The
  span is joined only to *test* balance and re-emitted verbatim.
- **A body that is itself a complete `if`/`for`/`case`.** `for (..) STMT` and
  `for (..) begin STMT end` are the same statement, so this is
  behaviour-preserving by construction.

  **A trailing `else` belongs to whichever construct can take one, and getting
  that backwards corrupted four more files.** If the body is an `if`, the `else`
  is the body's and must be pulled INSIDE the new block — left outside it would
  re-bind to an enclosing `if`. If the body is a `case`, `for` or `while`, that
  construct cannot take an `else` at all, so the `else` belongs to the *header
  being wrapped* and pulling it in orphans it. `khs_unit.v`, `kht_predec.v`,
  `kht_unit.v` and `mag_link_cdc.v` all broke on the second case in one pass.
- **A single-statement body WITH an `else` after it.** The old refusal assumed
  the block had to close as `end else` on one line, and a wrong merge moves a
  statement between branches. It does not have to: `end` on its own line, then
  the `else` untouched, is legal and is a pure insertion. Safe only on the
  single-statement path — `_one_statement` has already rejected a body that
  opens a block, so the `else` cannot be the body's, which is exactly the
  dangling-else case the complete-construct path above still refuses.
- **`else` alone on its line with a single-statement body below**, and
  **`else X;` where X runs on across lines.** Same reasoning, same test.
- **`if (C) A; else B;` on ONE line**, both arms a single simple statement.
  This is most of the benches' spin loops.

Those last three took the residue from 180 to 56 without a hand edit.

Two bugs in the driver itself, both of which read as "the fixer refused this
file" rather than as failures:

- **The `--fix` flags each rewrote the ORIGINAL text and the last one wrote.**
  `--fix-decls --fix-case` in one invocation wrote the decl result, then
  overwrote it with a case-only rewrite of the input. They are chained now.
- **One pass reaches one nesting level.** Wrapping `for (i..) for (j..) x;`
  leaves the inner `for` bare, so the passes run to a fixpoint.

### Two counts in this table were wrong, and the way they were wrong is the lesson

**F7 was reported as 185, then 188, then 0. It was 7** — one file in `src/attic`
and one in `src/reference/arithmetic`, both outside the scope anything had been
measuring, both now labelled. The counter had been matching every `begin` inside
a generate *region*, and `noc_l2_adapter.v` wraps its entire body in one
`generate if (PASS)`, so every `always ... begin` in the file counted. This is
the second time this exact check has measured the wrong thing; the first version
reported 385 for the same reason and was "fixed" by restricting it to generate
regions, which was not enough. The **0** was right about the live source and
wrong about the tree, for the scope reason above.

What it takes to be right: walk the region tracking whether each open block is
*procedural*, and note that an `always` whose body is
`if (..) begin .. end else begin .. end` opens **two** blocks at the same level —
so the always must stay "armed" across its whole statement, not just until the
first `begin`. Erring toward procedural undercounts, which is the safe direction.

**F4 was reported as 777, then 753.** Both were pre-fix numbers printed by a
`--fix` run, which computes counts before writing. The rule itself was also
under-specified at first: the original R2 regex saw only the next-line form and
missed `if (go) x <= 1'b0;` entirely, which is most of them.

**Read a style count as a claim that needs checking, not as a measurement.**

`scripts/py/vstyle.py` measures them; `--show` gives per-file rows worst first
and `--lines` every finding with its line. Three rules are mechanically
rewritable:

- `--fix-decls` (F3) re-packs one declaration's names onto full lines. No
  reordering, no reindentation of anything else.
- `--fix-blocks` (F4/F4a) wraps a single-statement body in `begin`/`end`, on the
  same line or the next. Still narrow: the statement must be bracket-balanced and
  end in one `;`, or be a complete `if`/`for`/`case` construct. `if (a) if (b) x;`
  is left alone, and that case is what the narrowness exists for.

  **A `--fix` pass can corrupt source, and this one did.** An earlier version
  accepted any line that *started* with `if (`/`for (` and *ended* with `)` as a
  bare header, then wrapped the line below as its body. `if (rd_take) rr_rd <=
  (rd_sel == N - 1)` matches both tests, so `begin`/`end` landed mid-expression
  in `mm_xfer.v`, `mag_dram_port.v` and `khs_unit.v`; ~50 of 103 benches went red
  and every failure traced to those three files. It was caught only because a
  wrong wrap happens to be a *syntax* error — one that stayed legal would have
  been silent. Run the suite after every `--fix` pass, and read one file's diff
  before running it over the tree.
- `--fix-case` (F5) shifts a flat case's whole body one level right, between
  `case` and its **matching** `endcase`. Moving the span together preserves
  relative indentation inside it, so a nested case that was already correct
  comes along at the right depth instead of being re-flattened.

Everything else is a human's. Apply a pass, then run
`check.py full --counts-baseline <ledger>`: 104 checks and their numbers are the
only thing separating a reformat from a rewrite. A one-character slip during
this work — a `\` typed for `//` — broke 30 benches and was invisible until the
suite ran.

**A file no bench compiles has nothing to catch a bad reformat**, and roughly
half of `src/reference`, all of `src/attic` and the frozen station-bus copies in
`tests/axi/build-jtagdbg/rtl/` are in that position. Those are gated by parsing
each file ALONE under `xvlog -sv`, which is what both fixer corruptions
presented as. Standalone parse is stricter than a bench build — a file needing a
macro from a sibling fails there and is fine in the suite — so compare the
failing SET before and after, never the count. One file fails today and did
before this pass: `src/reference/arithmetic/fp_exp.v` uses SystemVerilog `int`.

F6 is the one rule only half of which is checkable. The script sees the trailing
operator; whether an expression *should* have been broken at all is judgement,
and `format-example2.v` is where that judgement is shown rather than stated.

### The correction that matters

An earlier version of this file said R1 was *"one name per `localparam`
statement"* and counted 124. **That was wrong.** The hand-formatted half of
`format-example.v` keeps multi-name declarations:

```verilog
localparam [3:0] A_ADD = 4'd0, A_SUB = 4'd1, A_SLL = 4'd2, A_SLT = 4'd3;
localparam [3:0] A_SLTU= 4'd4, A_XOR = 4'd5, A_SRL = 4'd6, A_SRA = 4'd7;
localparam [3:0] A_OR  = 4'd8, A_AND = 4'd9;
```

Several names on a line is fine. What is banned is the **continuation line** —
the same ten constants wrapped across three lines with the `=` hand-aligned
read as a table and diff as a paragraph, because adding a name in the middle
re-aligns every line and the diff then touches all of them and says nothing.
Note that `A_SLTU=` above is already mis-aligned, which is what always happens.

Correcting the rule dropped the count from 124 to **83**, and fixing the F4
checker to catch the same-line form (`if (go) lsu_done <= 1'b0;`, which the
next-line-only regex missed entirely) raised its count from 204 to **777**.

One more: `strip_block_comments` used to delete a multi-line `/* .. */`
outright, which joined the code above it to the code below and shifted every
line number after it. Harmless while nothing reported a line; wrong the moment
`--lines` did. It replaces the comment with its own newlines now.

And the F7 walk is the only check that reads WORDS rather than line shapes, so
it was counting the word `begin` inside a `//` comment sitting in a generate
region. The two files that reported it were `format-example.v` and
`format-example2.v` — the normative pair, the only ones whose prose says the
word. Line comments are blanked before the walk now, and the fix is checked by
a probe with a real unnamed block beside such a comment: the block is reported,
the comment is not. **A check you have not watched fail is an assumption.**

### F6 is recursive, and applied per term

The one rule that is a judgement rather than a pattern. An expression that fits
stays on one line; one that does not is wrapped in its own parens with the
operator leading each continuation line. A **nested group breaks the same way
only when that group is itself complex** — too long for its line, or carrying
enough operators to be parsed rather than read — and each term is judged on its
own, so two shapes inside one group is correct:

```verilog
wire hazard_deep = (
    go
    && (
        ((st == S_ADDR) && p_gnt[0] && !s_last)
        || (
            (st == S_DATA)
            && (wptr != {WSEL_W{1'b0}})
            && s_last
        )
    )
    && !stall
);
```

At that depth the terms usually want names instead, which is what F1's "a line
that will not fit is an expression that wants a name" means.

### Applying it is per file, behind that file's bench

The rules add `begin`/`end` and parentheses and move line breaks; none of them
changes behaviour, and that is checkable rather than assumable. `kht_core.v`
reformatted to F1–F12 end to end ran `kht_sys` **cycle-identical** to the
unformatted source — 4184 cycles, 832 requests over 112 gathers, the same halt
word and cause, 4 checks. That is the bar for each file: reformat it, run its
bench, compare the numbers and not just the PASS.

At tree scale that bar is `--counts-baseline`, which applies it to all 104
checks at once instead of one file at a time. Take the ledger BEFORE the pass —
after it, there is nothing left to compare against.

### Verilog linting — available now

```
python scripts/py/vlint.py <bench> [--top <generated_top>]
python scripts/py/vlint.py <bench> --tb     # lint the testbench too
python scripts/py/vlint.py --list
```

`verilator --lint-only -Wall` over a bench's own source list from `xsim.py`, so
there is no second list to drift. Waivers are in the script with a reason each;
anything not waived is a finding. Verilator comes from
`micromamba create -n hdlfmt -c conda-forge verilator`.

Three things it needs to be honest, each of which cost a run to find:

- **Vendor libraries on the search path.** `DSP48E2` and the `xpm_*` macros are
  Vivado's, so without `-y` on `unisims` and `xpm_*/hdl` every module naming a
  primitive is `MODMISSING` and the run ends before reporting anything about our
  RTL.
- **`glbl` is unresolvable under lint.** `DSP48E2` reads `glbl.GSR`, a
  hierarchical reference to an instance that only exists once a simulation top
  instantiates one. It is fatal and it is in a vendor file, so the run continues
  past it and the exit code is decided by *our* findings only — otherwise the
  gate is permanently red and therefore useless.
- **Findings are classified by the LOCATION**, the path immediately after the
  warning kind — not by whether our tree is mentioned in the message. A vendor
  timescale warning names our file as the *includer*, and a substring test filed
  107 of them as ours.
- **"Is this finding ours" is not "has anything been printed yet."** The first
  version tested the output list for emptiness to decide whether to keep a
  finding's continuation lines, so once one of our findings had landed every
  later VENDOR finding's context lines were printed as ours too. It needs its
  own flag.

Current state on `ctrlpe_mesh`, our RTL only: **79 `PINCONNECTEMPTY`,
64 `PINMISSING`** — `GENUNNAMED` is clear (its 3 were labelled), and so are
`MODMISSING` and `WIDTHTRUNC`.

The 64 `PINMISSING` in `src/` are **7 source lines**, not 64: 8 `sb_hub`
instances in `sb_stn_line.v` of which one connects `stat_flits`/`stat_wait`,
4 `sb_link` in `sb_line4.v`, 4 in `mag_link.v`, 2 in `noc_orchestrator.v`, and
`DSP48E2`'s unused cascade pins in `vec_dsp.v`. In `src/` every one is an
unconnected **output**, which is the harmless direction.

### …and that is exactly why it was not enough

**`vlint.py` excludes `tests/`.** So the class that actually bit this tree was
never measured: `khs_facc_tb` and `khs_ffold_tb` both instantiate
`khs_float_lane` and neither connected its `op[4:0]` **input**. An unconnected
input is `z`, the op decode muxes on it, and every result is X — which reads as
a dead RAM, not a missing pin. Both benches had been failing that way, and the
symptom pointed at the accumulator while the fault was in the port map.
`khs_ffold_tb` additionally tied `do_zero` to `1'b0`, so a partial RAM that
reset does not clear was never initialised.

Two rules follow, and the second is the one that was missed:

- Write every port explicitly, `.port()` for the deliberately unused ones, so
  the linter can tell "unused" from "forgotten".
- **Lint the benches too.** A missing output pin in `src/` is cosmetic; a
  missing input pin in a bench is a false failure that costs a debugging
  session, and the tree has now paid for that twice.

### What linting has actually found

| file | finding | outcome |
|---|---|---|
| `vec_core.v` | `nchunk` computed 5 bits into 4 | **not a bug.** `O_VSETVL` already faults with `F_VL` unless `vl` is 1..128, so it cannot exceed 8. Rewritten as a 4-bit add with the invariant stated. |
| `vec_agu.v` | 3-bit index into a 4-entry array | safe under the `wr_fld <= 4` guard; now an explicit 2-bit `fld_sel`. |
| `mag.v` | `% MEM_PORTS` computing 32 bits into a narrow reg | explicit slice. |
| `mx_tdesc.v`, `mx_tcu.v` | 3 unnamed generate blocks | labelled. |
| `khs_facc_tb.v`, `khs_ffold_tb.v` | `khs_float_lane`'s `op` input unconnected | **the expensive one.** `z` into the op decode made every result X, both benches failed as "16 of 16 partials wrong", and the symptom pointed at the accumulator. Only found by probing the write-back path and measuring *200 pulses, 200 X*. `--tb` exists because of this. |
| `card.py` | `write32` undefined | real: `MeshClocks.set()` raised `NameError` and had never run. |
| `noc_l2_adapter.v` | refused a flit with `flags[4]` set | a spec violation — the bit is reserved and must be **ignored**, so the adapter was answering the same request differently from MAG. |

The `vec_core` row is the useful one in the other direction: a width warning
that looks exactly like a latent overflow, on code that is already correctly
guarded. **Read the guard before calling a width warning a bug** — and then
still fix the width, so the next reader does not have to.

And the honest summary of a day spent on this: of the failures chased, **three
were code** (the three rows above) and the rest were **stale tests, stale
generated artifacts, or a bench configured differently from the ship**. A test
that fails is a claim about the code; check which of the two is wrong before
fixing either.

## Standing rules that are not style

These are in `CLAUDE.md` and are not negotiable per-patch:

- **`sysnode`, never a plain "node".** A NoC endpoint is a node and every
  compute unit sits on one; the system node is MAG + mover + control processor.
- **One source list per bench, in one place** — `scripts/py/xsim.py`. Anything
  that needs a file list derives it from there.
- **Every bench prints `PASS` or `FAIL`** and has a watchdog that counts as a
  failure. A bench that can expire quietly has reported a pass it did not earn.
- **A bug found at system level is reproduced in the lowest-level bench that
  can hold it, before it is fixed.** A system-level repro is a location, not a
  fix.
