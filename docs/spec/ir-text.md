# IR text: one statement syntax, a vocabulary per level

Every IR level has a text, and no level's text is Python. The three texts share
one statement grammar; what a statement means is its level's VOCABULARY, which a
project extends with its own words (its unit ops, layouts, item kinds, L3 ops).
A level's text is complete: printing a level and reading the text back gives the
same IR, and so the same package.

Code: `compiler/kohakuaccel/text/` (the grammar `kat.lark`, `syntax.py`,
`vocab.py`, `machine.py`); each level's text beside its IR:
`kohakuaccel/ir/l1/text.py`, `kohakuaccel/ir/l2/text.py`,
`kohakuaccel/ir/l3/text.py`. KohakuTPU's words:
[../projects/kohakutpu/ir/text.md](../projects/kohakutpu/ir/text.md).

## 1. Statements

```
[target =] op args [: annotation]
    block statements, indented
```

- **One statement a line.** No statement spans lines, except that a newline
  inside `( )` or `[ ]` is not a statement end.
- A block is the statements indented under a statement (4 columns a level).
- `#` starts a comment to the end of the line.
- Arguments are apart by spaces or by commas; a statement prints back with the
  separator it was read with. `name=value` is a keyword argument; every other
  argument is positional.

| term | text | parses to |
|---|---|---|
| name | `x`, `reduce.max` (a dot followed by a letter) | `Name` |
| integer | `42`, `1_000`, `0x8010_0000`, `16K` (K/M/G) | `Int(value, style)` |
| float | `1.5`, `1e-05` | `Float` |
| dims | `64x64x2` | `Dims` |
| string | `"kohakutpu-l1"` (no quote, no newline inside) | `Str` |
| negation, not | `-x`, `!p` | `Neg`, `Not` |
| offset | `buf+0x40` | `Offset` |
| arithmetic | `c*64K + t*2K`, `(i%2)*4 + j`, `n/2 - 1` | `BinOp` (`-` `*` `/` `%`; `+` is `Offset`) |
| comparison | `i < 7`, `k == 0` | `Compare` |
| computed name | `v{16 + j}`, `e{s*4 + u}`, `v{4*q}[2:]` (name touching `{`) | `Computed`, folded to a `Name` or `View` when read |
| span | `0..H` | `Span` |
| call | `tiles(L, bq)`, `f(x: f16[N], n=2)` (name touching `(`) | `Call` of terms, `Typed`, `Kw` |
| view | `q[h, i, :]`, `m[:, *]`, `x[lo : hi]` (name touching `[`) | `View` of terms, `Slice`, `NewAxis` |
| tuple | `()`, `(x,)`, `(1, 0)` | `Tuple` |
| assignment | `n=128`, `0x10 = 1`, `o[h] = x` | `Assign` |
| membership | `h in 0..H` | `Member` |
| arrow | `->`, `<-` | `Arrow` |

A name and a bracket apart are two terms: `MG (1, 0)` is a name and a tuple.
Binary operators bind tighter than argument separation, so `a -b` is a
subtraction; a negative argument after another is written after a comma. A
binding's target may be computed: `q{i*4 + u} = mark vc0`.

**Canonical text.** The printer writes one form of each term: hexadecimal in
groups of four digits, sizes in K/M/G when whole, a one-binding statement as
`op name = value`, keyword arguments after positional ones, annotations of one
block aligned two columns past the longest statement (at most column 48), and a
blank line around each top-level block. Reading canonical text and printing it
gives the same text.

**Diagnostics.** A parse error and a meaning error alike are a `TextError`
naming the file, the line and the column, with the line and a caret.

## 2. Vocabularies

A `Vocabulary` maps a statement's keyword to a READER (`fn(reader, stmt) -> IR`)
and an IR class to a WRITER (`fn(writer, obj) -> Stmt | [Stmt]`, along the
class's MRO). A level's framework module owns the structure statements and a
vocabulary per extension point; a project registers words on those without
touching the grammar.

Every level's text opens with its head (`kohakuaccel/text/machine.py`):

```
level l1
machine "kohakutpu-l1"
unit mg0 MG (1, 0)
```

The machine is the caller's, or the text's name looked up in the project's
machines. A unit's name is its type in lower case and its index among that
type's coordinates in order; a reader refuses a `unit` line the machine does
not have.

## 3. L1 (`kohakuaccel.ir.l1.text.L1Text`)

A file of programs on one machine:

```
image img0                        # a definition block a project registers
    ...
program p0
    send mg0                      # one block a send; its lines the unit type's words
        ...
    t0 = mark mg0
    wait mg0 upto t0              # or `wait mg0`
    barrier
    move                          # the mover's words, or raw register writes
        write 0x10 = 1
```

Extension points: `units[TYPE]` (what a unit of the type is sent), `mover`,
`defs` (a top-level definition block `KEYWORD NAME`, bound for later lines; a
writer asks `L1Writer.define` for a definition's name and the file holds it
once). Each op read is lowered at once, so an op that does not encode is refused
at its line.

## 4. L2 (`kohakuaccel.ir.l2.text.L2Text`)

One schedule on one machine:

```
buffer a MxA(rows=128, k=128, gm=8, nk=2) at 0x8010_0000
buffer scratch at 0x8010_7800 bytes=16K
buffer state local=vc0 bytes=16K
package 0
    item gemm_tile on mg0               # a unit name when placed, a unit type when not, `mover`
        with a_at=a b_at=b+0x1000 gm=8 late=true srcs=(x,) body=("silu", 3, 2)
        read a[0 : 0x1000], b
        write c[0x800 : 0x1000]
```

- A layout is a registered frozen dataclass written as a call of its fields; a
  buffer's `bytes=` is printed only where it is not the layout's `nbytes`.
- A view is `buf` (the whole buffer) or `buf[lo : hi]` in bytes.
- A parameter is an int, a float, a string, `true`/`false`/`none`, or a tuple
  of those. An int inside a memory buffer prints `buf+off` (`buf` at offset 0)
  and reads back as the address; a name that is a buffer is its base.
- Items are in sequence order; consecutive items of one package share a block.
- Extension points: `layout(cls)`, `kind(name, unit, required, optional)`; an
  item's parameters are checked against its kind.

## 5. L3

The L3 program's text is [l3-program.md](l3-program.md) §2.

## 6. Modules

A module is one text holding a kernel at every level, the levels' bodies side
by side (`kohakuaccel/text/module.py`). Its top-level statements:

```
target ktpu.v9                       # the machine the L1 bodies address
fn NAME.LEVEL(p: TYPE, ...) [-> TYPE]
    body                             # LEVEL is l3, l2 or l1
image NAME(params)
    statements                       # a unit's program, its params constants
macro NAME(params)
    statements                       # text expanded where `expand NAME(args)` stands
```

- One name carries at most one body a level, and the bodies under one name are
  one kernel written at several levels: a compiler's output from the higher
  body is gated against the hand-written lower one.
- A body stays statements in the module; its level's reader gives it meaning.
  A project's reader for each level is its own.
- `image` and `macro` names share one table; a name defined twice is refused.

KohakuTPU's modules (suffix `.ktpu`), their L1 words and kernels:
[../projects/kohakutpu/ir/ktpu.md](../projects/kohakutpu/ir/ktpu.md).

**Read-time expansion** (`kohakuaccel/text/meta.py`). Before a level reader
sees a body, the expander rewrites it against an environment of constants (a
macro's or image's arguments, a fn's bound parameters, loop variables):

| construct | effect |
|---|---|
| `for v in LO..HI unroll` (block) | the block once per value of `v`; an L1 reader unrolls every `for` |
| `when COND` (block) | the block if the constant condition holds, else nothing |
| `expand NAME(args)` | the macro's statements with its parameters bound |
| arithmetic, comparisons | folded when both sides are constants; a free name plus constants stays one `Offset` |
| computed names | `v{i + 1}` to `v3`; a computed binding target likewise |

A name the environment binds to a number is that number; a macro argument that
is a name stays a name, and a statement whose op is such a parameter takes the
name as its op. A name on the left of `=` or `+=` (a keyword, a register) is
never substituted. The expanded body holds no meta statement.

## 7. Toolchain

The grammar is LALR(1) with a contextual lexer (lark), the indentation a
post-lexer. `kat.lark` is the grammar's one definition: a port to another
parser (pest behind pyo3 and wasm; tree-sitter for editors) is written from it
and is conformant when it reads the texts of `compiler/tests/text/` to the same
statements.

## 8. Gates (`compiler/tests/text/`)

- Every hand-written L1 kernel printed, read back: the same steps and the same
  package bytes; the printed text a fixed point.
- Every hand-written L2 schedule printed, read back: the same L1 text and the
  same packages from the L2 -> L1 compiler.
- Hand-written texts (`golden/words.l1`, `golden/schedule.l2`) against Python
  transcriptions of them: a witness of each word that is not the printer.
- Every L3 reference kernel read, verified, printed to a fixed point, and run
  by the reference interpreter against independent numpy references.
- Each kind of wrong text refused at its line.
