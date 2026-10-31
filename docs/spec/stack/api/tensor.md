# API: the `kp` tensor API, buffer forms and storage

`kohakutpu.application.api` (`kp`) is the machine's own tensor library: a
lazy graph planned by the API itself, each realised tensor one generated DSL
kernel. Its buffers, forms and allocation are the runtime's
(`application/runtime/`), shared with the tinygrad device (`tinygrad.md`).
Clause format, ids and evidence tags: `../README.md`.

Evidence on this page:

- `silicon`: the runtime piece is unchanged since the 2026-10-21 card run
  (tree `af7c1bb`; `git diff af7c1bb` changes `runtime/forms.py`,
  `runtime/rows.py`, `kernels/*.py` and the docstring of
  `runtime/storage.py`; `runtime/device.py` and `runtime/call.py` are
  unchanged) and the LeNet run through the tinygrad device necessarily took
  it (the 455 kernels of the 2026-10-21 card runs). A refusal or error path is
  never `silicon`. The `kp` API itself never ran on the card.
- `RTL`: `software/application/tests/ktpugrad/test_tinygrad.py` on the
  Verilated card, current tree, 76 of 80 passing (the 4 misses are MXFP7 precision),
  exercising the shared runtime piece.
- `unproven`: neither. `software/application/tests/api/test_tensor.py` runs
  on the Verilated card, but no run of it on the current tree is recorded
  (`docs/projects/kohakutpu/api.md:76-81` records an earlier one), so every
  API-only clause is `unproven`.

Machine facts are machine-schema parameters with the v9t6 value in brackets:
`die.word_bytes` [32], `vc.lanes` [16 f16 a word], `vc.count` [2],
`mg.count` [2]. A product column block is `vc.vlmax / mg.lanes` [32]:
one vector register across the cluster's GT4 row band. The stack uses a
widest block of 256 columns,
and a statistic word of `vc.lanes` values.

## 1. Tensors

- **API.TEN.001** `unproven` `kp.tensor(a, dtype=np.float16)` uploads `a` (made contiguous, cast to `dtype`) as a realised tensor; an element type other than f16 / f32 is refused (`Unsupported`: "the card computes on f16 / f32"). `kp.zeros(*shape)`, `kp.ones(*shape)`, `kp.full(shape, v)`, `kp.arange(n)` upload f16 host arrays. (code: software/application/kohakutpu/application/api/tensor.py:382-387; api/__init__.py:30-43; doc: docs/projects/kohakutpu/api.md:18-25)
- **API.TEN.002** `unproven` A tensor is realised (a buffer) or lazy (a node: an expression over `Ref` reads of realised tensors and at most one `Prod` read, on a rows view). `realize()` runs the node's one kernel; `numpy()` realises and reads back. (code: tensor.py:55-77, :105-172)
- **API.TEN.003** `unproven` Every realised result is f16: the output storage's element type is float16 whatever the inputs (an f32 input is read through its f16 form, API.FORM.003). (code: tensor.py:596-598)
- **API.TEN.004** `unproven` `numpy()` of a statistic reads its f16 values from the current form (API.FORM.009) as `shape`; of a row tensor, its current form compacted to `size * itemsize` bytes; of a transposed view, the base's array with the last two axes swapped. (code: tensor.py:163-172)
- **API.TEN.005** `unproven` `reshape(*shape)` realises and returns a tensor over the same buffer (numpy's reshape rules, `-1` allowed) keeping the statistic flag; `flatten()` is `reshape(size)`. A reshape of a transposed view is refused ("a reshape of a transposed view: only matmul reads one"). (code: tensor.py:174-187)
- **API.TEN.006** `unproven` `.T` of a rank >= 2 tensor is a view of the last two axes swapped, no bytes moved, readable only as matmul's right operand; `.T` of a view returns its base; rank < 2 is refused. (code: tensor.py:189-200)
- **API.TEN.007** `unproven` `bool(t)` is refused ("read it with .numpy() first"). (code: tensor.py:259-260)
- **API.TEN.008** `unproven` `kp.device()` is the runtime device (opened once, `runtime.device.device()`); `kp.runs()` lists every kernel run so far (name, card cycles, wall time, kind, info). (code: api/__init__.py:70-77; runtime/device.py:66-74, :257-262)

## 2. Operations

- **API.OP.001** `unproven` Elementwise ops build a lazy node: `+ - * /` (and reflected), `**` (`x ** 2` is `x * x`, else `pow`), unary `-`, `abs`, `<`, `>`, `<=` (`not >`), `>=` (`not <`), `eq`, `ne`, `maximum`, `minimum`, `where(a, b)` (a where self is nonzero), `exp2`, `log2`, `exp` (`exp2(x log2 e)`), `log` (`log2(x) ln 2`), `sqrt`, `rsqrt`, `reciprocal`, `sin`, `cos` (`sin(x + pi/2)`), `trunc`, `square`, `relu` (`max(x, 0)`), `sigmoid` (`1 / (1 + exp2(-x log2 e))`), `tanh` (`2 sigmoid(2x) - 1`), `silu`, `gelu` (the tanh form), `clip(lo, hi)`. A number operand is a constant. (code: tensor.py:202-323; doc: api.md:31-44)
- **API.OP.002** `unproven` Operand shapes broadcast as numpy's; shapes that do not broadcast are refused ("they do not broadcast"). (code: tensor.py:427-434)
- **API.OP.003** `unproven` Reductions `sum`, `max`, `min` (`-max(-x)`), `prod`, `mean` (sum times 1/n), `var` (population: mean of squared deviations), `std` (`sqrt(var)`) take `axis` (the last only) and `keepdim`; the result is a row statistic of shape `shape[:-1]` (+`(1,)` with `keepdim`). `axis=None` reduces the whole tensor through a `[1, size]` view (shape `()` or all ones with `keepdim`). (code: tensor.py:325-351, :460-481)
- **API.OP.004** `unproven` `softmax(axis=-1)`, `log_softmax(axis=-1)` (last axis only), `layernorm(weight, bias, eps=1e-5)`, `rmsnorm(weight, eps=1e-6)` are built of the ops above over the last axis. (code: tensor.py:353-371)
- **API.OP.005** `unproven` `matmul(a, b)` / `a @ b` takes `b` as the `.T` view of an [N, K] (or batched [.., N, K]) tensor; the result is `a.shape[:-1] + (N,)`; `kp.linear(x, w, b=None)` is `x @ w.T (+ b)`. A batched B's batch shape equals A's. (code: tensor.py:484-508; doc: api.md:42-43)
- **API.OP.006** `unproven` A product's N is padded to `Np = ceil(N / block) * block`, where `block = vc.vlmax / mg.lanes` by the same product planner as the tinygrad device. B's padding rows quantise to zero. The logical result keeps N columns; a narrow result keeps its padded-rows form and read-back strips the padding. N not divisible by the block width is supported. (code: tensor.py `_matmul`; ktpugrad/run.py `_product`, `_product_run`; tier-0 vector: RUL.028.product_n_padded)
- **API.OP.007** `unproven` A product's operands are packed MXFP7 on the card (A and B packings, API.FORM.006) and the epilogue (bias, activation, residual: the elementwise math over the product) runs on the vector cores in the same kernel (`kind` "matmul"). (code: tensor.py:659-690)

## 3. Fusion and kernels

- **API.FUSE.001** `unproven` `kp.set_fusion(on)` sets fusion and returns the previous setting; with fusion off every op is its own kernel (a 4-op chain: 4 kernels). (code: tensor.py:28-36; doc: api.md:45-47; test: software/application/tests/api/test_tensor.py:94-102)
- **API.FUSE.002** `unproven` With fusion on, a lazy operand is inlined when it has the same rows view and: it adds no second distinct product, no product next to a reduction (either side), and, for a view wider than the stack's widest block (256 columns), no reduction when the consumer is elementwise or already holds one. Otherwise the operand is realised first and read as a `Ref`. (code: tensor.py:397-424)
- **API.FUSE.003** `unproven` A realised tensor is exactly one kernel launch path: a product (`_matmul`), a statistic (`_statistic`) or rows (`_wide`); a 4-op elementwise chain, a row sum, softmax, layernorm with weight and bias, `std(-1, keepdim=True)`, `where`, `gelu`, `x[:, :77] * 3` are one kernel each. (code: tensor.py:578-598; test: test_tensor.py:48-84)
- **API.FUSE.004** `unproven` A statistic of a lazy reduction keeps that reduction's rows view, so a chain over statistics fuses with it. (code: tensor.py:444-453)

## 4. Reads (operand kinds)

- **API.READ.001** `unproven` A kernel over a rows view reads each operand as `full` (the whole [.., W] view), `col` (one row every row reads), `row` (one value a row: a statistic) or `scalar` (one value); a rank lower than the view's is padded with leading ones. Any other broadcast is refused ("kernels read whole rows, one row, a value a row or one value"). (code: tensor.py:512-530)
- **API.READ.002** `unproven` A statistic read as a row of values (not `keepdim`) is refused ("keep the reduced axis (keepdim=True)"), both when broadcasting against a non-statistic of a different last extent and when read as a `full` / `col` operand. (code: tensor.py:436-441, :541-545)
- **API.READ.003** `unproven` Statistics of statistics run over R values with W 32, each input value a row (16 a word). (code: tensor.py:533-538, :642-645)

## 5. Row layouts

- **API.ROW.001** `RTL` A rows kernel computes over [R, W] f16 rows with W whole 32-column blocks (the stack's block width). (code: software/application/kohakutpu/application/kernels/vector.py:115-116; inventory A-012; evidence: test_tinygrad.py reductions `sum_rows`, `max_rows`, `softmax` (W 64))
- **API.ROW.008** `unproven` A rows kernel of any other W is refused by the generator ("a GT4 row is whole 32-column blocks"). (code: kernels/vector.py:115-116; evidence: refusal, no card or RTL run)
- **API.ROW.002** `RTL` Rows are read in GT4 bands: 4 rows x a column block a load, one 4 x 32 block a register; a column block is the widest multiple of 32, at most 256, that divides W and fits L1 (W = 96 gives 96; F-022), a trip is 1 to 4 bands double-buffered in a core's L1 (`vc.l1_words` [512] less a 16-word margin), 16 rows when a statistic is read or written. (code: kernels/vector.py:24-28, :82-131; inventory A-010, A-011, A-013, A-014, A-015; evidence: test_tinygrad.py `sum_rows`, `max_rows` (a statistic written, 16 rows a trip), `softmax`, `layernorm`)
- **API.ROW.003** `RTL` A row wider than one block (the stack's widest block, 256 columns) with one reduction runs as per-block partials `[blocks, R]`, folded by halving the blocks (an odd middle block carried by a copy), then the expression around the reduction over the folded statistic. `rows.SINGLE_PACKAGE_AST` (default on, from `ktpu_kernels::gen::reductions::defaults()` through `_rs.ROWS_DEFAULTS`; `KTPU_SINGLE_PACKAGE_AST` overrides it) makes the partials, folds and expression one `reductions.wide_fold_kernel` launch, each partial still stored as f16 in its scratch, and a reduction down the rows one `reductions.column_tree_kernel`; off, they are separate `rows` / `copy` launches, one a tree level. The Rust planner takes the same default (`ktpu_plan::run::Knobs::rows`). (code: software/application/kohakutpu/application/runtime/rows.py:55-131, :176-194, runtime/launch_reductions.py; ktpu-kernels gen/reductions.rs; evidence: test_tinygrad.py `sum_wide_rows`, `max_wide_rows`, `sum_far_rows`)
- **API.ROW.004** `RTL` A rows kernel's last partial band (R not a multiple of the quantum) is computed in a zeroed scratch band: each operand's rows copied in by the mover, the band computed there, its first rows copied out; no row past R is read or written except a statistic's last word, whole. (code: kernels/vector.py:306-343; evidence: test_tinygrad.py `sum_cols`, `max_cols`: the halving tree (runtime/tree.py:73-117) runs its last levels and its head as rows kernels of R = 2 and 1)
- **API.ROW.009** `unproven` A row-wide operand outside the reduction is refused ("a row-wide operand outside a reduction whose result is a statistic"); any other reduction across blocks is computed first as a statistic. (code: runtime/rows.py:55-131, :176-194; evidence: refusal; no passing case is known to take the statistic-first path)
- **API.ROW.005** `unproven` In `kp`, rows of W not whole 32-column blocks run as a flat kernel when every operand is `full` and none is a statistic; a broadcast operand there is refused ("rows of W (not whole 32-column blocks) with a broadcast operand"), and a reduction of such rows is refused ("whole 32-column blocks"). (code: tensor.py:610-627, :646-650)
- **API.ROW.006** `RTL` A flat kernel computes N elements as 128-value rows (8 words), every operand at the output's index; trips start at 16 rows, halving until two buffers of every operand fit L1; the rest runs as one trip of whole groups of 4, then 2, then 1 rows; no row past the last is read or written. (code: kernels/vector.py:346-401; inventory A-019; evidence: test_tinygrad.py elementwise cases `add`, `affine`, `relu`, `exp` (TG.RUN.013))
- **API.ROW.010** `unproven` A flat kernel operand may be one value (its word, the lane read by `unpack lane=`). (code: kernels/vector.py:352-378; evidence: no passing card test is known to make one (`docs/spec/stack/dsl/graph.md` conflict 9))
- **API.ROW.007** `unproven` A stage's n trips split over at most `node.vc` vector-core instances from the machine schema, every trip covered exactly once, by the one split `ktpu_kernels::gen::vector::defaults().trips` names (Python `vector.TRIPS` through `_rs.VECTOR_DEFAULTS`): balanced (the default, RUL.016) gives core c `[ceil(c*n/p), ceil((c+1)*n/p))` over `p = min(n, node.vc)`, counts differing by at most one; hchunk gives core c `[c*h, min((c+1)*h, n))` with `h = ceil(n / min(n, node.vc))`, over `ceil(n / h)` cores, which can be fewer than p. An empty stage has one instance with zero trips. Generator core and cluster counts are required arguments from the machine, with no defaults. (code: kernels/vector.py `split`; tier-0: test_vector.py `test_split_covers_every_trip_once`, `test_split_traces_every_row_once`, vc = 1..4; vector: RUL.016.split_over_every_core)

## 6. Forms

- **API.FORM.001** `silicon` A buffer (`Storage`) is its row-major bytes (form `flat`) plus side forms; the forms a kernel writes are the valid ones until the next write: `wrote(key)` makes `key` the only valid form and drops the host bytes, `made(key)` adds a form made from a valid one. (code: software/application/kohakutpu/application/runtime/storage.py:15-77; doc: api.md:49-58; evidence: host path of every LeNet kernel: each tinygrad buffer is a `Storage` (TG.DEV.005) and every run marks what it wrote; storage.py's code unchanged since af7c1bb)
- **API.FORM.002** `silicon` A form a kernel needs is made on the card by a conversion kernel. (code: storage.py:7-9; runtime/forms.py:174-230; evidence: the MXFP7 packings of the LeNet products made by `quant` kernels, tally `quant` 172 in 25 kernels)
- **API.FORM.003** `silicon` A kernel reads f16; an f16 buffer is read at its own bytes. (code: forms.py:62-71; inventory A-046; evidence: every LeNet tensor is f16 (`DEFAULT_FLOAT=half`, `nn.place`, TG.PIN.001, TG.NN.001) and every vector kernel in the tally reads f16 (`vunpk ... f16` 8131))
- **API.FORM.004** `silicon` A padded-rows form `("rows", n, np)` holds rows of n f16 values np apart, np whole 32-byte words; producers use np = ceil32(n) (the rows kernel's W', the product's Np). (code: forms.py:24-38; software/application/kohakutpu/application/ktpugrad/run.py:1479-1481, :2001; inventory A-087; evidence: LeNet's products of N 120, 84 and 10 columns run at Np 128, 96, 32: the tally's mm kernels `out: f16[64, 128]`, `f16[64, 96]`, `f16[64, 32]`; the code is unchanged since af7c1bb)
- **API.FORM.005** `RTL` A padded-rows form is made row-major (`compact`) by `repitch`: one mover walk when the row and both pitches are whole words, else the lane relayout (exact, the destination between its rows 0). For an f32 buffer the compacted values land in its f16 form. (code: forms.py:135-171; evidence: test_tinygrad.py `n84_dense_operand`; offline plan witnesses: `software/rust/crates/ktpu-plan/tests/run.rs`)
- **API.FORM.006** `silicon` An MXFP7 packing is a form keyed `("mx7", side, source operand, rows, cols, rp, cp, g, nk, batches)`: `batches` [rows, cols] blocks quantised by the mover into [batches * rp, cp], zero-padded, tiles of `g` lane groups (A: tm/4, B: tn/4), kept while the buffer is unwritten. (code: forms.py:174-230; software/application/kohakutpu/application/kernels/matmul.py:67-115; evidence: the card-run tally `quant` 172 in 25 kernels, e.g. `fn quant_11e4cccd11.l1(x: f16[6400, 400] @dram, q: mx7a[6400, 416] @dram(gm=64, nk=1), ...)`)
- **API.FORM.007** `unproven` Rows whose column count, pitch or offset is not whole words are first laid out ceil16(cols) apart by `forms.relayout` (the extra columns zero), then quantised; rows of a buffer held as padded rows (its row-major bytes not current) are laid out from those rows, never compacted first (`forms._held_rows`, test_padded_reads.py). (code: forms.py:211-221; evidence: test_tinygrad.py `k10`, `n10` exercise it and miss the 2e-2 tolerance (0.0239, 0.0216; the MXFP7 precision misses TG.TOL.002 records), so no passing test witnesses it)
- **API.FORM.008** `silicon` A folded f16 constant table is uploaded once a device by content, at least 32 bytes. (code: forms.py:267-277; inventory A-089; evidence: the transpose kernels' `mask` table (kernels/transpose.py `masks()`, 38 kernels) and LeNet's folded index tables, through the af7c1bb `table`, whose f16 path is the same)
- **API.FORM.009** `silicon` Read-back takes the row-major bytes when they are current (or any sub-buffer's bytes), else (with no f16 form) the padded-rows form with the padding dropped. (code: forms.py:280-286, :290-295; evidence: LeNet's loss (`loss.item()`, mnist_mlp.py:105), and its evaluation logits (`model(..).numpy()`, mnist_mlp.py:113): `l3`'s product of N 10 held in rows 32 apart (the tally's mm `out: f16[64, 32]`))
- **API.FORM.013** `unproven` A form the machine cannot make is refused (`FormError`); bytes cross the link only on upload and read-back (conflict 8). (code: storage.py:7-9; forms.py:41-42; evidence: refusal; the host-side cast of conflict 8 contradicts the second part)
- **API.FORM.014** `RTL` An f32 buffer is read through its f16 form (values rounded once), made from the host bytes it was uploaded from. (code: forms.py:77-85; evidence: test_tinygrad.py `f32_input`)
- **API.FORM.015** `unproven` Without host bytes an f32 buffer's f16 form is made on the card by a cast kernel from its f32 bytes; with neither, `FormError`. (code: forms.py:86-90; evidence: no passing card test is known to take the cast; refusal)
- **API.FORM.016** `RTL` A table may hold another element type (`table(dev, arr, dtype)`), its content key holding the shape and element type. (code: forms.py:267-277; evidence: the lane relayout's fp32 index lists (`forms.relayout`), test_tinygrad.py `narrow_transpose`, `odd_permute`)
- **API.FORM.017** `RTL` Read-back takes, when the row-major bytes are not current, the f16 form widened before the padded-rows form. (code: forms.py:287-289; evidence: test_tinygrad.py `f32_input` (an f32 output held in its f16 form, TG.DEV.010); every LeNet buffer is f16)
- **API.FORM.019** `unproven` Read-back of a buffer with no current form is `FormError`. (code: forms.py:296; evidence: refusal, no card or RTL run)
- **API.FORM.010** `RTL` A row statistic in memory is its R f16 values, 16 a word (`vc.lanes` [16] f16 values), `ceil(R/16) * 32` bytes; a kernel reading or writing one moves 16 rows a trip. This is the only statistic memory form (RUL.030), shared by the tensor API and tinygrad. Full row outputs explicitly broadcast the compact value into register rows before a normal row pack. (code: software/application/kohakutpu/application/runtime/rows.py:232-235; kernels/vector.py:42-53, :279; inventory A-003, A-048; evidence: test_tinygrad.py reductions)
- **API.FORM.011** `RTL` GT4 is the clusters' drained sub-tile layout (`tile=(tm/4, tn/4)`): a product's tile scratch is read by the epilogue as `tile` operands in GT4 bands. (code: kernels/matmul.py:127-167; kernels/vector.py:183-201; evidence: test_tinygrad.py `linear_relu`, `batched_n48`)
- **API.FORM.012** `RTL` `for_port(spec)`: an f16 port takes the tensor's f16 form; an MXFP7 port a packing of the tensor's last two axes. (code: forms.py:233-264; evidence: f16: test_tinygrad.py `test_a_dsl_kernel_inside_a_tinygrad_program` (softmax's f16 ports); MXFP7: the `linear`, `mlp`, `swiglu`, `attention`, `splitk` examples through `runtime.call` (call.py:58), test_examples_card.py)
- **API.FORM.020** `RTL` A storage records which valid forms have zero padding (`Storage.zeroed`: the elements a row pitch adds), claimed only by the writer of those elements and cleared by any write. An MXFP7 packing of whole rows held as zero-padded padded rows reads `ceil16(cols)` columns in place (the extra columns are the zeros it pads with): no relayout. (code: K/runtime/storage.py `Storage.zeroed`; K/runtime/forms.py `packed`, `_held_rows`; evidence: test_layout_hints.py `test_second_run_reads_the_producers_rows`, mutant `padding not known zero` caught)
- **API.FORM.021** `RTL` Producers follow their consumers' layout hints (`forms.hints`, per device; `LAYOUT_HINTS` off: every producer writes its row-major bytes). A packing that pads a producer's whole rows to whole words hints the producer's site "rows". Next time, a bare gather of that site writes its rows as zero-padded padded rows of whole lines (`ROW_ALIGN` 32 values), and a rows kernel of that site stores its padding lanes as zeros. A compaction of a site's padded rows hints it "flat" for good. The choices are learned from the run's own consumers, so the same model run makes them offline and on the card. A capture runs `CAPTURE_STEPS` (2) steps so that the second step's kernels are in the library. (code: K/runtime/forms.py `hints`, `packed`, `compact`; K/ktpugrad/run.py `_gather`, `_rows_out`, `_rows`, `_zero_lanes`; scripts/py/templates/prebuild.py `CAPTURE_STEPS`; evidence: test_layout_hints.py offline counts (fewer relayouts in the second run, as many in the third, none fewer with hints off) and on the model (values, mutant `unzeroed padding` caught); ViT-tiny per image 36 -> 12 converts, LeNet b8 per step 141 -> 131)
- **API.FORM.022** `RTL` A library holds the layout hints its captures learned (`forms.HINTS_FILE`, sites by their text: shapes, dtypes and expressions, never addresses). A device of that library starts from them (`forms.seed_hints`), so a captured model's first step runs its steady layouts and the library classes captured for them. A capture starts from its library's hints too and runs the model's program (LeNet: a forward, the training steps, the evaluation forward after them) in passes until a pass changes no hint (at most `CAPTURE_PASSES`). Every hint state a run of that program reaches, seeded or not, is one a pass started from or passed through, so the library holds every kernel such a run makes. What it learns is merged into the library's hints (`forms.save_hints`): a site any capture hints "flat" stays flat. (code: K/runtime/forms.py `Hints`, `hints`, `seed_hints`, `library_hints`, `save_hints`; scripts/py/templates/prebuild.py; evidence: test_layout_hints.py `test_library_hints_make_the_first_run_steady` (first run's relayouts = steady run's, every case), mutant `library hints unread` caught, `test_save_hints_keeps_a_flat_site_flat`)
- **API.FORM.018** `unproven` An f32 port takes the f32 bytes of an f32 buffer whose row-major bytes are valid; an MXFP7 port of a tensor of rank < 2 is refused; anything else is `FormError`. (code: forms.py:240-264; evidence: no card test binds an f32 port; refusals)

### Prepared operands and current-form reads

A deferred operand (`forms.Pending`) holds relayout steps, not readable primary
bytes. With `forms.PREPARED` on, a rows consumer emits those
steps inside its own kernel. Quant-side preparation (`forms.PREPARED_QUANT`)
folds a pending packing only where the cost profile admits its recipe
(`runtime.costs`); with no profile it never does. Both defaults have one
source, `ktpu_kernels::gen::prepared::defaults()` (`_rs.PREPARED_DEFAULTS`;
`KTPU_PREPARED`, `KTPU_PREPARED_QUANT` override them per process), read by
`forms` and by the Rust planner (`ktpu_plan::run::Knobs::forms`), which takes
the same prepared, made and profile-gated paths. `forms.COMPOSED`, of the same
source, reads a pending operand on its consumer's loads and packs where that
is taken (`docs/spec/stack/l2/prepared-consumers.md`). Unrepresentable steps fall
back to explicit relayout launches. A repeated rows operand is prepared once; each reader retains
its own offset. The optional single-package reduction path materializes pending
operands before entering its emitter.

Every reader selects a valid storage form. In particular, `_laid_read` obtains
the current dense f16 form after its bare-read producer before repitching;
a producer may have written padded rows according to its consumers' hints.
Read-back of a subview uses its logical element offset in the current F16,
padded-rows, or word-walk view form, not an invalid primary allocation.
Witnesses: `test_prepared.py` ordered model regression and stale-read mutant;
`test_current_read.py` f16/f32 subviews crossing padded rows.

Fusion acceptance uses CPU f32 references: maximum absolute error divided by
maximum absolute reference value (floor 1e-9), below 0.03 for the tested MXFP7
products and 0.002 for f16 reductions. Nonfinite results fail. Reduction order
may differ; stale reads and incorrect aliases are not numerical tolerance.

## 7. Storage and allocation

- **API.MEM.001** `silicon` `Storage(dev, nbytes)` allocates its row-major bytes from the device; a side form is allocated when first asked or when its size grows (contents then invalid) and freed with the storage. A `StorageView` is a byte range of a storage, flat form only. (code: storage.py:26-96; evidence: host path of the LeNet run: every tinygrad buffer and sub-buffer (TG.DEV.005), the MXFP7 side forms of API.FORM.006; storage.py's code unchanged since af7c1bb)
- **API.MEM.002** `unproven` Allocation extents are exact; a kernel must not access beyond its buffer's word-rounded extent (RUL.020). Allocation adds no tail; the handle exposes word-rounded requested bytes, independently of placement alignment. Flat/cast and reverse tails compute in declared scratch and copy only their destination words. RUL.020.no_allocation_tail is frozen; adjacent-word sentinel vectors run on the small model. See [memory lifetimes](../l0/memory.md).
- **API.MEM.003** `unproven` `kp` allocates an elementwise result of [R, W] as `max(32, 2 (R + ceil4(R) - R) W)` bytes and a statistic as `ceil(R/16) * 32` bytes. (code: tensor.py:569-575)
- **API.MEM.004** `silicon` An upload writes whole host lines (the board's write granule or the transport line, else 32 bytes) and keeps the host bytes for other forms; a read rounds up to whole lines. (code: runtime/device.py:170-181; software/application/kohakutpu/application/runtime/call.py:27-34; inventory A-006; evidence: host path of LeNet's uploads (weights, images) and loss reads over XDMA; device.py and call.py unchanged since af7c1bb)
- **API.MEM.005** `frozen` Allocation pressure collects the oldest submitted kernel and retries after each completion. With no submissions remaining it drops unused spilled packages and retries once. (code: runtime/device.py `_take`; tier-0 vector: API.MEM.005.oldest; mutant: memory/drain-all)
- **API.MEM.006** `unproven` Recording allocates on the main heap and pins every allocation used by a recorded access or submission. The returned `Recorded` log owns its pins until closed or destroyed; replay refuses a closed log. (code: runtime/device.py; tier-0 vector: L0.BIND.030.recording; mutant: memory/no-record-pins)
- **API.MEM.007** `unproven` A submission allocates its scratch and retains its input and output allocations, including interior views. Successful collection drops those references and scratch ownership; failed waits preserve reservations. Owner free alone never releases memory still referenced by work or recording pins. (code: runtime/device.py; tests: application/tests/runtime/test_lifetime.py)
- **API.MEM.008** `unproven` A `kp` buffer is freed when the last tensor viewing it is collected. (code: tensor.py:43-52)

## 8. Your own kernel

- **API.CALL.001** `unproven` `kp.call(kernel, *tensors, **consts)` binds the tensors to the input ports in order (a count mismatch is `ValueError`), specialises for f16 / f32 specs of their shapes on the device's machine, hands each over in its port's form (API.FORM.012), allocates each output (`spec.nbytes()`), runs once (kind "custom") and returns every output as a new tensor in port order. (code: software/application/kohakutpu/application/api/custom.py:16-59; doc: api.md:60-67)
- **API.CALL.002** `unproven` A transposed view or a statistic as a kernel input is refused; an output that is not row-major f16 / f32 is refused ("a Tensor holds row-major f16 / f32"). (code: custom.py:23-27, :39-44)
- **API.CALL.003** `unproven` `kp.function(kernel)` returns a callable whose call returns the output of a one-output kernel (else `ValueError`: "call .outputs(...)") and whose `outputs(...)` returns every output. (code: custom.py:62-87; test: test_tensor.py:105-108)
- **API.CALL.004** `RTL` `runtime.call.call(dev, kernel, arrays, **consts)` runs any DSL kernel on numpy f16 / f32 arrays: uploads, makes port forms, runs, reads every output back (row-major f16 / f32 as arrays), frees what it allocated. (code: runtime/call.py:15-79; evidence: test_examples_card.py, record in docs/projects/kohakutpu/dsl.md:116-134)
- **API.CALL.005** `unproven` `runtime.call.call` refuses an array of another element type (`TypeError`) and a missing input (`ValueError`); an output not row-major f16 / f32 comes back as raw bytes. (code: runtime/call.py:19-24, :41-45, :74-75; evidence: no example has such an output; refusals)

## 9. Refused, by clause

Every `Unsupported` carries a stable `clause` attribute and includes that id
in its diagnostic. Tests assert the clause, not the explanatory message.
The refusal clauses are:

- **API.REF.001** `unproven` `x @ w` with `w` a row-major [K, N] tensor (not a `.T` view): refused by API.REF.001; store the weight [N, K] and write `x @ w.T`. (code: tensor.py:487-491; doc: api.md:71-73; test: test_tensor.py:114)
- **API.REF.002** `unproven` A reduction, `softmax` or `log_softmax` over an axis other than the last: refused by API.REF.002. (code: tensor.py:375-379; test: test_tensor.py:115)
- **API.REF.003** `unproven` A row statistic broadcast as a row of values: refused by API.REF.003; keep the reduced axis. (code: tensor.py:436-441; test: test_tensor.py:116-117)
- **API.REF.004** `unproven` matmul with K differing, A of rank < 2, or mismatched batch shapes: refused by API.REF.004. (code: tensor.py:493-496)
- **API.REF.005** `unproven` A transposed view as an elementwise operand, a reduction operand or matmul's left operand: refused ("a transposed view is read only as matmul's right operand"). (code: tensor.py:390-394, :417, :461, :499)
- **API.REF.006** `unproven` A reduction over every row statistic or along a statistic's rows: refused ("reduce the rows themselves"). (code: tensor.py:463-473)
- **API.REF.007** `unproven` A generated kernel's own refusal (`RowsError`, `ValueError` from planning) surfaces as `Unsupported` with clause API.REF.007 and an explanatory message, the output storage freed. (code: tensor.py:630-634, :653-655, :665-668)

- **API.REF.008** `unproven` Accessing a lazy tensor's storage is refused until it is realised.
- **API.REF.009** `unproven` Reshaping a transposed view or transposing a tensor of rank less than two is refused.
- **API.REF.010** `unproven` Converting a device tensor to a Python truth value is refused.
- **API.REF.011** `unproven` A tensor element type other than f16 or f32 is refused.
- **API.REF.012** `unproven` Elementwise shapes that do not broadcast are refused.
- **API.REF.013** `unproven` An operand outside the supported full, column, row and scalar broadcast forms is refused.
- **API.REF.014** `unproven` A custom kernel input that is a transposed view or statistic, or an output other than row-major f16/f32, is refused.

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.016, RUL.020, RUL.025, RUL.027, RUL.028, RUL.029, RUL.066, RUL.067, RUL.094.
