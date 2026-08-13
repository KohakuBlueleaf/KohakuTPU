# Examples

**How to use the card.** For what it can do, see `demos/kohakutpu/`.

Every file is labelled with the level it is written at, and touches only that
level and the one next to it. When a file names something three levels down —
an address, a node coordinate, a flit — that is a missing API, not a shortcut.
See `.plan/LEVELS.md`.

Run any of them directly. **They go to the unit models in `kohakutpu.model` by
default, not to the card** — the same kernels through the same artifact. Reaching
hardware takes `--device card`, or `KOHAKUTPU_DEVICE=card`, and is a decision
rather than a fallback: a script that said nothing used to open whatever was
attached, in the middle of somebody else's measurement. Every demo and example
takes the same `--device`, from one helper in `kohakutpu.api`.

| | Level | What it teaches |
|---|---|---|
| `01_tensors.py` | 5 | Arrays live on the card, an op is a call, a result is a tensor. **Read this first.** |
| `02_kernel.py` | 4 | Writing kernels for both unit types under one `@kernel`, and a fused two-stage one. |
| `03_dynamic_shapes.py` | 4 | One trace, five shapes. Structure is early, shapes are late. |
| `04_tuning.py` | 4 | `gm`/`gn`/`nk`: what they change, and what they leave alone. |
| `05_ranks.py` | 4 | `...`, so one kernel serves `(L, D)`, `(B, L, D)` and `(B, H, L, D)`. |
| `06_memory.py` | 5 | Lifetime through the only controls an application gets. |
| `07_layout.py` | 2 | The byte orders, and why elementwise work commutes with them but a reduction does not. |
| `08_instructions.py` | 1, 0 | The bottom, by hand. **Not how to use the card** — here so the levels above are an abstraction *of* something. |
| `09_tinygrad.py` | 5, 4 | The same computation three ways, with what each one cost. **Level 5 is not one API** — and the refusals are half the file. |

`09` needs tinygrad installed. `demos/kohakutpu/tinygrad_mlp.py` runs a whole
two-layer MLP through it.

## Level 5 has more than one entry point

`kohakutpu.api.Array` and tinygrad are **peers** at this level, not layers: both
are tensor APIs and what they share is level 4 and below, the kernel library.
You pick one.

tinygrad buys the scheduler's fusion — `(x @ w.T).silu()` is one kernel and
matches `linear_silu` byte for byte, which an eager API cannot do without being
told the kernel's name. It costs an extra host round trip per kernel, and it
**refuses** everything the library has no kernel for: the bias fusion, `relu`,
`softmax`, the norms, attention, and every elementwise chain. A refusal raises
and names the ops it saw; it never falls back to computing on the host.
`09_tinygrad.py` measures all of that rather than asserting it.

## The shape of it

Level 5 — what to compute:

```python
from kohakutpu import api as ktpu

x, w = ktpu.tensor(X), ktpu.tensor(W)
y = ktpu.silu(ktpu.matmul(x, w))
print(y.numpy())
```

Level 4 — how one instance's work is shaped:

```python
@kernel
def matmul(a=L.In(M, K), b=L.In(N, K), c=L.Out(M, N), *, gm=2, gn=1, nk=2):
    with units(a.tiles(gm), b.tiles(gn)) as (i, j):
        ra, rb = L.region(gm, nk), L.region(gn, nk)
        acc = L.tile(gm, gn)
        for k in loop(a.chunks(nk)):
            ra <<= a[i, k]
            rb <<= b[j, k]
            acc += ra @ rb
        c[i, j] <<= acc


y = matmul(x, w)  # a call, not a launch
```

There is no compile step, no encode step, no upload step, no launch step and no
gather step in either. The extents come from the arguments, the result is
allocated by the compiler, the operands are packed into whatever byte order this
compilation asked for, temps are placed and folded scalars are materialised.

If you ever find yourself writing a launcher, something below has failed to
decide something and pushed the decision up to you. That is a bug in the stack,
not a thing to work around.
