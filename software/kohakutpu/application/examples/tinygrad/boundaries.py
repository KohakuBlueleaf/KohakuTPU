"""Explicit operation boundaries for eager-versus-lazy example measurements."""

from tinygrad.helpers import TRAINING


def boundary(tensor, realize_ops, *, contiguous=False):
    """Retain the original materialized movement only on the eager side."""
    if not realize_ops:
        return tensor
    if TRAINING:
        # A scheduler barrier retains the backward graph until gradients are built.
        return tensor.contiguous()
    return (tensor.contiguous() if contiguous else tensor).realize()
