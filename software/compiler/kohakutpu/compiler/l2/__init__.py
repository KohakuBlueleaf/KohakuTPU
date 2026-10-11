"""KohakuTPU's L2 (docs/spec/l2-program.md): `fn NAME.l2` bodies read,
narrowed by the L2 -> L2 passes, lowered to L1 text, and the L1 images
list-scheduled (L1 -> L1); `pipeline.compile_l1` runs them in order."""
