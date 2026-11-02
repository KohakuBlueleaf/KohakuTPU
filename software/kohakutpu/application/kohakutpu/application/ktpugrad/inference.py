"""Same-shape forward calls use TinyJit/KTPUGraph; no model-specific paths."""

from collections import OrderedDict
from itertools import cycle

from tinygrad.engine.jit import TinyJit
from tinygrad.helpers import Context
from tinygrad.tensor import Tensor

from kohakutpu.application.runtime import device as D
from kohakutpu.language.config import ENV

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
            ENV.get("KTPU_INFERENCE_REPLAY", "1") == "1" if replay is None else replay
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


class Resident:
    """One resident copy of a forward per node, sharing its weights in DRAM.

    Each copy keeps its own captured graph, slots and scratch, so requests on
    two nodes share no written span and overlap on the card. Read a result
    to wait for it.
    """

    def __init__(self, forward, nodes=None, replay=None):
        count = D.device().nodes if nodes is None else nodes
        nodes = range(count) if isinstance(count, int) else count
        self.copies = {n: Inference(forward, replay) for n in nodes}

    def on(self, node, *inputs):
        """One request on `node`, queued without waiting."""
        with D.device().on_node(node):
            return self.copies[node](*inputs)

    def __call__(self, requests):
        """Each request's inputs on the next node in turn: the results."""
        nodes = cycle(self.copies)
        return [self.on(next(nodes), *inputs) for inputs in requests]

    def reset(self):
        for copy in self.copies.values():
            copy.reset()


def shards(size, nodes):
    """`(node, start, stop)` per owning node of `size` entries, in even
    contiguous ranges; a node past the last entry owns none."""
    each, extra = divmod(size, len(nodes))
    out, at = [], 0
    for k, node in enumerate(nodes):
        stop = at + each + (k < extra)
        if stop > at:
            out.append((node, at, stop))
        at = stop
    return out


class Sharded:
    """One forward over a batch: each node owns a range of the batch axis.

    Every input's first axis is the batch; each range runs as a request of
    a `Resident` and the outputs join on that axis.
    """

    def __init__(self, forward, nodes=None, replay=None):
        self.resident = Resident(forward, nodes, replay)

    def __call__(self, *inputs):
        hosts = [x.numpy() for x in inputs]
        size = len(hosts[0])
        outs = [
            self.resident.on(
                node, *(Tensor(h[a:b], device=x.device) for x, h in zip(inputs, hosts))
            )
            for node, a, b in shards(size, list(self.resident.copies))
        ]
        return outs[0].cat(*outs[1:], dim=0).realize()

    def reset(self):
        self.resident.reset()


__all__ = ["Inference", "Resident", "Sharded", "shards"]
