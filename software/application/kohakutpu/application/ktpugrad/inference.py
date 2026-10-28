"""Same-shape forward calls use TinyJit/KTPUGraph; no model-specific paths."""

import os
from collections import OrderedDict

from tinygrad import TinyJit
from tinygrad.helpers import Context

from kohakutpu.application.runtime import device as D

SIGNATURES_KEPT = 8


class Inference:
    """A forward-only callable with bounded shape/dtype/device specialisations.

    Inputs are realised contiguous tensors. Parameters stay unchanged while this
    wrapper lives; call reset after replacing parameters. Like TinyJit, the result
    is reused by subsequent calls: read it before the next call or clone it.
    """

    def __init__(self, forward, replay=None):
        self.forward = forward
        self.replay = (
            os.environ.get("KTPU_INFERENCE_REPLAY", "1") == "1"
            if replay is None
            else replay
        )
        self.jits = OrderedDict()

    def _forward(self, *inputs):
        return self.forward(*inputs).realize()

    def __call__(self, *inputs):
        with Context(TRAINING=0):
            inputs = tuple(x.contiguous().realize() for x in inputs)
            if not self.replay:
                return self._forward(*inputs)
            signature = (
                D.device(),
                tuple((x.shape, x.dtype, x.device) for x in inputs),
            )
            jit = self.jits.get(signature)
            if jit is None:
                jit = self.jits[signature] = TinyJit(self._forward)
                while len(self.jits) > SIGNATURES_KEPT:
                    self.jits.popitem(last=False)
            self.jits.move_to_end(signature)
            return jit(*inputs)

    def reset(self):
        """Release captured graphs before changing the forward's parameters."""
        self.jits.clear()


__all__ = ["Inference"]
