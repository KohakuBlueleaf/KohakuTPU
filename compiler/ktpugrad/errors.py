"""What this frontend refuses.

Two kinds, and the distinction is the whole design: the matcher not recognising
a tree is ordinary, and the chain generator refusing one is the boundary past
which nothing here will guess. Neither ever falls back to the host.
"""


class KTPUUnsupported(NotImplementedError):
    """A kernel tinygrad scheduled that nothing on this backend computes."""


class ChainRefused(KTPUUnsupported):
    """An expression the elementwise chain generator will not spell.

    Carries the UOp kinds it saw, so the refusal names what was actually
    scheduled rather than what was expected.
    """
