"""Automatic address-parametric capture at a concrete eager realization boundary."""

import os
from collections import Counter

from tinygrad import tensor
from tinygrad.engine import realize
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.application.runtime import device as D
from kohakutpu.compiler import _rs
from kohakutpu.language.dsl.kernel import _MUTANT

ORIGINAL = tensor.transform_to_call
KEPT = 128
ENABLED = False


def refresh():
    global ENABLED
    ENABLED = os.environ.get("KTPU_EAGER_CALL_CAPTURE", "0") == "1"
    return ENABLED


refresh()


class Captures:
    def __init__(self, native=None):
        self.entries = {}
        self.placeholders = {}
        self.counts = Counter()
        self.native = (
            os.environ.get("KTPU_NATIVE_CAPTURE", "0") == "1"
            if native is None
            else native
        )

    def clear(self):
        self.entries.clear()
        self.placeholders.clear()

    def normalize(self, sink):
        symbolic, actual, inputs = {}, {}, []
        for node in sink.toposort():
            if node.op is Ops.BUFFER:
                slot = len(inputs)
                inputs.append(node)
                desc = (slot, node.device, node.max_numel(), node.dtype)
                if desc not in self.placeholders:
                    self.placeholders[desc] = UOp.new_buffer(
                        node.device, node.max_numel(), node.dtype
                    )
                sym = self.placeholders[desc]
            else:
                sym = node.replace(src=tuple(symbolic[s] for s in node.src))
            symbolic[node] = sym
            actual[sym] = node
        return symbolic, actual, inputs

    def native_call(self, sink):
        if len(self.entries) == KEPT:
            self.clear()
        key, nodes, inputs = _rs.capture_graph(sink, Ops.BUFFER)
        if key in self.entries:
            self.counts["hit"] += 1
            template, mappings, parameters, fresh = self.entries[key]
            replacements = {parameter: nodes[index] for index, parameter in parameters}
            replacements.update(
                {
                    node: UOp.new_buffer(node.device, node.max_numel(), node.dtype)
                    for node in fresh
                }
            )
            packed = template.instantiate(tuple(replacements.values()))
            return packed.src[0], {
                nodes[index]: value
                for index, value in zip(mappings, packed.src[1:], strict=True)
            }
        self.counts["miss"] += 1
        call, mapped = ORIGINAL(sink)
        indices = {node: index for index, node in enumerate(nodes)}
        if not all(node in indices for node in mapped):
            self.counts["generated keys"] += 1
            return call, mapped
        parameters = tuple(
            (
                index,
                UOp.new_buffer(
                    nodes[index].device, nodes[index].max_numel(), nodes[index].dtype
                ),
            )
            for index in inputs
        )
        replacements = {nodes[index]: parameter for index, parameter in parameters}
        values = UOp.sink(call, *mapped.values())
        fresh = []
        keep_cold = _MUTANT.get() == "app.capture/reuses_cold"
        for node in values.toposort():
            if node.op is Ops.BUFFER and node not in replacements and not keep_cold:
                replacements[node] = UOp.new_buffer(
                    node.device, node.max_numel(), node.dtype
                )
                fresh.append(replacements[node])
        self.entries[key] = (
            _rs.CaptureTemplate(
                values.substitute(replacements),
                [parameter for _, parameter in parameters] + fresh,
            ),
            tuple(indices[node] for node in mapped),
            parameters,
            tuple(fresh),
        )
        return call, mapped

    def call(self, sink):
        if self.native:
            return self.native_call(sink)
        if len(self.entries) == KEPT or len(self.placeholders) >= KEPT * 32:
            self.clear()
        symbolic, actual, inputs = self.normalize(sink)
        key = symbolic[sink]
        if key in self.entries:
            self.counts["hit"] += 1
            template, mappings, fresh = self.entries[key]
            replacements = {symbolic[node]: node for node in inputs}
            replacements.update(
                {
                    node: UOp.new_buffer(node.device, node.max_numel(), node.dtype)
                    for node in fresh
                }
            )
            packed = template.substitute(replacements)
            return packed.src[0], {
                actual[node]: value for node, value in zip(mappings, packed.src[1:])
            }
        self.counts["miss"] += 1
        call, mapped = ORIGINAL(sink)
        if not all(node in symbolic for node in mapped):
            self.counts["generated keys"] += 1
            return call, mapped
        # A template never owns an input/output allocation from the cold invocation.
        values = UOp.sink(call, *mapped.values())
        buffers = [node for node in values.toposort() if node.op is Ops.BUFFER]
        cold = [node for node in buffers if node not in symbolic]
        if _MUTANT.get() == "app.capture/reuses_cold":
            cold = []
        replacements = {node: symbolic[node] for node in buffers if node in symbolic}
        replacements.update(
            {
                node: UOp.new_buffer(node.device, node.max_numel(), node.dtype)
                for node in cold
            }
        )
        fresh = tuple(replacements[node] for node in cold)
        self.entries[key] = (
            values.substitute(replacements),
            tuple(symbolic[node] for node in mapped),
            fresh,
        )
        return call, mapped


def transform(sink):
    if not ENABLED or realize.capturing:
        return ORIGINAL(sink)
    nodes = sink.toposort()
    if sink.variables() or any(
        node.op in (Ops.CALL, Ops.FUNCTION, Ops.PARAM) for node in nodes
    ):
        return ORIGINAL(sink)
    buffers = [node for node in nodes if node.op is Ops.BUFFER]
    if not buffers or any(node.device != "KTPU" for node in buffers):
        return ORIGINAL(sink)
    dev = D.device()
    if dev.eager_calls is None:
        dev.eager_calls = Captures()
    return dev.eager_calls.call(sink)
