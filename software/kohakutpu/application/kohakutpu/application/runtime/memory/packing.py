"""Device-side FP16 packing contract and its collected failure."""

import json
from functools import lru_cache

from kohakuaccel.driver.node.queue.client import NodeError


@lru_cache(maxsize=64)
def policy(machine):
    return json.loads(machine)["machine"]["conversion"]["fp16_mx7"]


class NonfinitePackingError(NodeError):
    """A packing kernel observed a live FP16 exponent field of 31 on the card."""

    def __init__(self, completion, console=""):
        super().__init__(completion, console)
        self.args = ("NONFINITE_FP16: vector MX7 output is invalid; " + self.args[0],)

    def __reduce__(self):
        return (type(self), (self.completion, self.console))


def contract_error(error):
    """Preserve queue diagnostics while naming the project contract code."""
    c = error.completion
    if c.status != 0x24 or c.detail != 1 or isinstance(error, NonfinitePackingError):
        return error
    named = NonfinitePackingError(c, error.console)
    named.__dict__.update(error.__dict__)
    return named
