# L3 program: tiles of tensors in a project's hardware ops

L3 is what a kernel computes, written by hand or emitted by a front end (the
Python DSL, the tensor runtime): tensors in, tiles of them through ops that are
each one hardware feature, outputs stored back. It names no unit, no address
and no schedule; the L3 -> L2 compiler chooses those. A project supplies the
ops and dtypes (`OpSet`); KohakuTPU's are in
[../projects/kohakutpu/ir/l3.md](../projects/kohakutpu/ir/l3.md).

Code: `compiler/kohakuaccel/ir/l3/` -- `nodes.py` (the IR), `text.py`,
`verify.py`, `interp.py` (the numpy reference), `ops.py` (`OpSet`, shared shape
rules), `instance.py` (a program at concrete shapes, §5). The pass pipeline's
old graph IR is `kohakuaccel.ir.l3.legacy`, kept until its callers are gone.

## 1. Contents

| node | text | what |
|---|---|---|
| `Program(name, params, body)` | `program attention(q: mx7[H, L, 64], ...)` | a kernel; parameters are tensors |
| `Fn(name, params, ret, body)` | `fn silu(x: f16[M, N]) -> f16[M, N]` | a function, its body ending in `return` |
| `Tile(name, value)` | `tile bq = 32` | a named constant dimension |
| `Output(name, type)` | `o = output : f16[H, L, 64]` | a tensor the program writes and returns |
| `Let(name, op, args, attrs, type)` | `s = mmt q[h, i, :], k[h, j, :] : f16[bq, bk]` | one op |
| `Invoke(name, fn, args, inline, type)` | `a = inline silu(c)`, `a = call silu(x[i, :]) : f16[bm, N]` | a function applied |
| `Carry(name, init, type)` | `m = carry -inf : f32[bq]` | a value the next `scan` carries |
| `Map(vars, body)` | `map h in 0..H, i in tiles(L, bq)` | independent iterations |
| `Scan(var, domain, body)` | `scan j in tiles(S, bk)` | ordered iterations |
| `Next(name, value)` | `next m = mn` | a carry's value for the next step |
| `Store(target, value)` | `store o[h, i, :] = oi`, `store y = r` | into an output |
| `Return(value)` | `return y` | a function's result |

A type is `dtype[d0, d1, ...]`, or `dtype` for a scalar; a dimension is an int,
a parameter's shape symbol, or a tile name. An operand is a name, a constant
(`1.0`, `-inf`), a dimension name (its value, a constant), or a view.

**Views.** `x[e0, e1, ...]`, one entry a dimension from the first, missing
trailing entries whole: `:` whole; an int or a point variable drops the
dimension; a tile variable is a `size` slice of a dimension its domain tiles
(the verifier checks the domain's extent is that dimension); `lo : hi` a range;
`*` a new dimension of 1 (for broadcasting).

**Domains.** `lo..hi` points (hi excluded); `tiles(extent, size)` `extent/size`
tiles, `size` an int.

## 2. Semantics

- **Single assignment, block scope.** A name is bound once and never shadowed;
  a name bound inside a `map` or `scan` body is local to it.
- **Rounding.** Every op computes exactly and its result is rounded to the dtype
  its statement names: that is the value the hardware stores. A value moved
  into a carry, an output or a function parameter of another dtype is rounded
  to that one; of the same dtype it moves unchanged.
- **map** iterations are independent; a result is the same in any order. Only
  `store` leaves a map body.
- **scan** runs its points in order. The carries bound in the same block since
  the previous scan are its carries: each starts at its `init`, each step's body
  updates each exactly once with `next`, and after the scan the name holds the
  last value.
- **inline** and **call** compute the same value. `inline` asks the compiler to
  unfold the function into the caller; `call` keeps it one function compiled
  once and entered at each use. A function's shape symbols bind at each use.
- **Ops** are the project's; an op is one hardware feature, never a library
  function. What a front end composes from several is a `fn`.

## 3. What the verifier refuses (`verify.verify`)

- a name used before it is bound, or bound twice;
- an op the project does not have; the wrong number of operands; an attribute
  the op does not take; an operand of a dtype the op's unit does not read; a
  result dtype the op does not give;
- an inferred shape (broadcasting, the op's rule) different from the one the
  statement names; a statement with no type where one is needed;
- a view with too many indices, an index outside its dimension, a tile variable
  on a dimension its domain does not tile, a range that is not two ints;
- a carry with no scan after it, or updated other than once; a `next` outside
  its scan's body;
- a store into anything but an output, or of the wrong shape;
- a function that calls itself (directly or not), a `return` not last, a
  return of another type than the function's.

Each problem is a `TextError` at its statement's line.

## 4. The reference interpreter (`interp.run`)

`run(module, ops, program, inputs)` binds the parameters' shape symbols from
the inputs, rounds each input to its parameter's dtype, runs the program in
float64 with the rounding of §2, and returns the outputs. It is the meaning of
an L3 program: the L3 -> L2 compiler's output is graded against it.

## 5. A program at concrete shapes (`instance.instantiate`)

What a project's L3 -> L2 compiler reads. `instantiate(module, program,
shapes)`:

- binds every dimension symbol from the inputs' shapes and the `tile`
  statements, and every loop domain to ints (`Loop(kind, vars, body)`, a var
  ``(name, "span", lo, hi)`` or ``(name, "tiles", extent, size)``);
- expands `inline` and `call` alike -- they compute the same value (§2) -- the
  function's values named ``site.name``, its shape symbols bound from the
  arguments' shapes, an argument of another dtype than its parameter rounded
  by a `copy` (§2's rounding);
- resolves every operand to an `Operand`: a constant (a literal or a
  dimension's value), or a tensor (parameter, output) or value with its index
  entries (``("full",)``, ``("new",)``, ``("at", i)``, ``("var", name)``,
  ``("range", lo, hi)``);
- drops every statement no `store` or `next` reads.

The result is an `Instance`: parameters and outputs with their dtypes and
shapes, every value's dtype and shape, and the body as `Stmt`, `Loop`, `Put`
(a store), `CarryInit` and `Update` (a `next`).
