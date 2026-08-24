"""SDXL's blocks, following sd-scripts' structure and diffusers' parameter names.

The names matter: the checkpoint ships in diffusers layout, so a module that
calls its projection `to_q` loads without a translation table.
"""

from sdxl.model.attention import BasicTransformerBlock, CrossAttention, FeedForward

__all__ = ["BasicTransformerBlock", "CrossAttention", "FeedForward"]
