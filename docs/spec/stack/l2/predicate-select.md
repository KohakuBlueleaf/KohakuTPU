# Direct predicate selection

A comparison used only by one select need not materialize a floating-point
0/1 mask. The typed vector lowering recognizes the adjacent sequence
`compare p1; mask = 0; mask = 1 if p1; result = select(true, false, mask)`.
It emits `result = false; compare p1; result = true if p1` instead.

The two result writes and the predicate read retain their dependencies through
register allocation and scheduling. The rewrite runs before bank rotation in
band lowering and before modulo scheduling of stream iterations. Removing one
mask instruction and its live register lets the existing scheduler overlap more
independent work; it does not remove synchronization edges or add DMA queues.

Admission is based on instruction operands and use counts, never kernel names.
The mask must have no other use. An in-place destination must not overwrite
a comparison operand or the later true arm. Comparison metadata retains its
logical destination symbol even when both operands are constants: the allocator
requires destination-first symbol lists independently of textual operands.

The `l2.predicate-select/off` mutant provides the unmodified measurement
schedule. `l2.predicate-select/wrong-arm` substitutes the false payload in the
predicated write and must fail the numerical witness. The model witness compares
before and after to the same expected FP16 values, including subnormals and
nonfinite selectors. Signed zero follows the existing vector datapath's zero
canonicalization, not a promise of raw-bit FP16 transport.
