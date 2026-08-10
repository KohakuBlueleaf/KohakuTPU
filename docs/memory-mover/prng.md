# The noise generator

A counter-based PRNG beside the mover, reached as `GENERATE` mode. It fills a
region with random numbers and reads no memory at all.

Sources for the survey in §3 and §5 are at the end.

---

## 1. Why it is here and not on a vector core

`isa/vector.md` §3.1 excludes integer and bitwise arithmetic from the vector
core **on purpose**, and every counter-based PRNG is exactly integer and bitwise
work. Putting one in a lane means putting an integer datapath in a float lane,
for one op.

The mover is already the unit that writes a region from a descriptor. A
generator is that with the read side removed.

## 2. What needs it

- **Diffusion.** The initial latent is nothing but noise, and every ancestral
  step (Euler a, DDPM) needs a latent-sized draw. Today that is a host upload
  per step.
- **Training.** `limits.md` §4.3 makes this the prerequisite for dropout, which
  needs a host upload the size of the activations, every step.

## 3. What GPUs, TPUs and NPUs actually do

**Every one of them is counter-based and stateless.** This is settled practice,
not a choice left open. The value is a pure function of `(key, counter)`:

```
   value[i] = f(key = seed, counter = base + i)
```

Introduced as **Random123** (Salmon et al., SC11) and now the default
everywhere: **Philox-4x32-10** is in cuRAND, Intel MKL, MATLAB and PyTorch;
**Threefry** is the one XLA uses on TPU; JAX exposes `threefry2x32`,
`philox2x32` and `philox4x32` and passes the key explicitly rather than keeping
global state. Philox passes TestU01's BigCrush, and one engine is 44 bytes of
state, which is what makes a million parallel streams free.

### 3.1 Philox or Threefry: it is a DSP-versus-LUT question

The two differ in exactly one way that matters to us:

| | construction | costs |
|---|---|---|
| **Philox** | multiply-high/low, 32x32 -> 64 | **multipliers** |
| **Threefry** | add-rotate-xor, more rounds | **fabric**, no multiplier |

That is why *"the ThreeFry algorithm is fast on TPU but slow on CPU/GPU
compared to Philox"* -- a TPU has no spare integer multiplier, so it pays in
rounds instead.

**We are not a TPU in that respect, so take Philox.** The measured budget says
which resource is scarce here: the vector core is **fabric-bound, not DSP-bound**
-- 128 lanes extrapolate to ~160k LUT against 384 DSP, which is ~37% of an SLR's
LUTs against 12.5% of its DSPs (`compute/vector-core.md` §13). Philox spends the
resource we have and saves the one we do not. A 32x32->64 multiply is a small
number of DSP48E2s.

Threefry stays the fallback if the generator is ever replicated enough that DSPs
start to bind, and the interface does not change if it is swapped.

**What was built spends fabric anyway, and the argument above is why that is
still fine.** At a 320 MHz target the 32x32 product could not stay whole: a
DSP48E2 cascade's exit into fabric is 3.34 ns, which held both `mm_prng` and
`mm_mover` at 299.5 MHz, and no pipeline stage *around* the multiply helps
because the multiply is the segment. `M` is a constant, so it splits into two
16x32 partials with a register between them and their sum -- and at that width
Vivado builds the partials in LUTs and uses **no DSP at all**: 296 -> 1,089 LUT,
8 -> 0 DSP, 299.5 -> 333.0 MHz (`arch.md` §10).

So the DSP-versus-LUT premise did not decide the final circuit. The choice
survives on the second argument rather than the first: one generator's 1,089
LUTs are 0.06% of the device, and Threefry would have bought a fabric saving
that was never needed while costing rounds. If the generator is ever replicated
per lane, this measurement -- not the DSP count -- is the one to re-run.

### 3.2 What counter-based buys, stated as requirements

- **Determinism independent of scheduling.** Two runs that tile the same region
  differently produce identical noise. A stateful LFSR makes the result depend
  on visit order, so a scheduling change would look like a numerical bug.
- **Restartability.** A faulted move, reissued, produces the same bytes.
- **Parallelism.** Widening the generator is just handing each lane a different
  counter.

`base_counter` is derived from the **destination descriptor's linear position**,
so a region regenerated with the same seed is bit-identical however it was
walked. That is §6's first test and it is the property the whole choice exists
for.

## 4. Uniform: use the exponent trick, not a fixed grid

The naive conversion -- take N mantissa bits and scale by `2^-N` -- gives a
uniform on a **fixed grid**, so the smallest nonzero value is `2^-N` and
everything near zero has terrible relative resolution. At FP16 that is `2^-11`.

`scripts/py/random_float_from_int.py` already does the right thing instead:
**pick the binade geometrically, then fill the mantissa uniformly.** Walk random
bits from the LSB and decrement the exponent while they are set, so `[0.5, 1)`
gets probability 1/2, `[0.25, 0.5)` gets 1/4, and so on; then draw the mantissa.
Every dyadic interval gets its correct measure *and* full mantissa precision, at
every scale.

That is the reference implementation for `GENERATE` without `NORMAL`, and the
RTL should be checked against it directly.

**For `NORMAL`, do not build a float at all.** Feed the raw 32-bit integer to
§5's tables. The float question only arises when the caller asked for a uniform.

## 5. Normal: Box-Muller, and a correction

An earlier draft ruled Box-Muller out on the grounds that this machine has no
`cos`. **That was wrong, and the error is worth naming**: it confused the
*vector core's* op set with what a dedicated unit can carry. The generator has
its own tables, exactly as the vector core's four transcendental seeds are its
own tables generated by `scripts/py/vec_tables.py`.

Both GPU and hardware practice land on Box-Muller for the same reason:

| method | shape | verdict |
|---|---|---|
| **Box-Muller** | fixed latency, no branching, 2 in -> 2 out | **this one** |
| Ziggurat | rejection-acceptance, **variable output rate** | wrong for streaming hardware |
| inverse CDF (probit) | 1 table, but unbounded and steep at both ends | the fallback |
| sum of uniforms | tails wrong past 3 sigma | too crude for a latent |

*"For GPUs, the Box-Muller transform turns out to be the best choice for the
Gaussian transform because it avoids branching and looping issues that affect
other methods"*, and rejection methods have an output rate that is not constant,
*"making it less desirable to a hardware simulation environment"*. There is a
published FPGA Gaussian noise generator built on exactly this.

### 5.1 It factors into two tables, which is why it is cheap

```
   r     = sqrt(-2 ln u1)          one table, in u1 directly
   c, s  = cos(2*pi*u2), sin(...)  one table pair, quarter-wave symmetric
   z0    = r * c
   z1    = r * s                   two normals from two uniforms
```

`sqrt(-2 ln u)` is fitted **as one function of `u`** rather than as a `ln` and a
`sqrt` in series -- it is smooth and grows gently, which is a far easier fit
than probit, whose steepness at both ends forces a domain split. Two lookups and
two multiplies, fixed latency, no rejection.

The pairing is exact: **Philox-4x32 emits four 32-bit words, which is two
`(u1, u2)` pairs, which is four normals.** The datapath has no leftovers.

### 5.2 The two corners that produce an infinity

- **`u1 = 0` gives `ln 0`.** Map the integer 0 to `2^-32`, or use
  `(i + 1) / 2^32`, so the domain is `(0, 1]`. This is the classic Box-Muller
  bug and it produces an `inf` that propagates into a whole latent.
- **`u2` needs the full circle**, so the angle table must cover `[0, 2*pi)`; a
  quarter table plus quadrant logic is fine and is the usual construction.

### 5.3 Tail depth, and how much is enough

With a 32-bit `u1`, the deepest radius is
`sqrt(-2 ln 2^-32) = sqrt(44.4) ~= 6.7 sigma`.

How much is needed is a **model** question, and this repo has already done the
arithmetic that answers it: `compute/vector-core.md` §11.1 notes that **4.2 is
the expected maximum of 65,536 standard normals.** A `128 x 128 x 4` latent is
65,536 elements, so a step draws roughly that many and the extreme sits near
4.2 sigma. **6.7 sigma is comfortable; the fixed grid of §4 at FP16 would not
be.**

Record the achieved tail beside the table when it is fitted.

## 6. Verification

In order. Each catches something the next one cannot.

1. **Positional determinism.** Generate a region in one move; generate it again
   as four moves covering quarters. Require identical bytes. **Write this
   first** -- it is the property the counter-based choice exists for, and
   nothing else in the ladder notices it failing.
2. **Philox against its published test vectors**, checking the Python model
   first so the model is trustworthy before the RTL is compared to it.
3. **The transform against the generated table model**, bit for bit. The table
   is generated, so this is an equality, not a tolerance -- the same discipline
   as `vec_tables.py`.
4. **Distribution** on a large sample: mean, variance, and a
   Kolmogorov-Smirnov statistic against the normal CDF. Catches a wrong table;
   does **not** catch a wrong counter mapping, which is why (1) comes first.
5. **The `u1 = 0` corner explicitly**, per §5.2.

## 7. Open

- Philox round count. Ten is the published default; fewer rounds still pass
  BigCrush in some analyses, and this is a measurement, not an argument.
- Elements per cycle. One 256-bit beat at FP16 is 16 elements; Philox-4x32 gives
  four words per invocation, so beat rate needs four instances or four cycles.
  This matters only if noise generation shows up against DRAM bandwidth, which
  it should not.
- Whether `GENERATE` should also apply a scale and offset. It is one FMA and
  would remove a vector pass for `x * sigma + mu`, but it is the first step onto
  the slope `isa.md` §6 refuses.
- Whether the uniform path needs the §4 exponent trick at all, or whether every
  real caller wants `NORMAL`. If nothing asks for uniform floats, the simpler
  fixed-grid conversion is enough and §4 becomes a note.

---

## Sources

- [Parallel Random Numbers: As Easy as 1, 2, 3 (Salmon et al., SC11)](https://www.thesalmons.org/john/random123/papers/random123sc11.pdf)
- [Random123 library documentation](http://www.thesalmons.org/john/random123/releases/1.11.2pre/docs/)
- [How PyTorch Generates Random Numbers in Parallel on the GPU](https://blog.codingconfessions.com/p/how-pytorch-generates-random-numbers)
- [TensorFlow: Random number generation (Philox everywhere, ThreeFry on XLA/TPU)](https://www.tensorflow.org/guide/random_numbers)
- [JAX: Pseudo-Random Number Generation on TPU](https://docs.jax.dev/en/latest/pallas/tpu/prng.html)
- [jax.random module (threefry2x32, philox4x32 key and counter spaces)](https://docs.jax.dev/en/latest/jax.random.html)
- [AMD GPUOpen: Sampling From a Normal (Gaussian) Distribution on GPUs](https://gpuopen.com/learn/sampling-normal-gaussian-distribution-gpus/)
- [A Hardware Gaussian Noise Generator Using the Box-Muller Method (Imperial College)](http://www.doc.ic.ac.uk/~wl/papers/06/tc06dul.pdf)
- [NVIDIA GPU Gems 3, Ch. 37: Efficient Random Number Generation Using CUDA](https://developer.nvidia.com/gpugems/gpugems3/part-vi-gpu-computing/chapter-37-efficient-random-number-generation-and-application)
- [cuRAND documentation](https://docs.nvidia.com/cuda/archive/13.0.3/curand/group__HOST.html)
