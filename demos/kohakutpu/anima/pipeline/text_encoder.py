"""Text encoder interfaces for Anima on KohakuTPU."""

import numpy as np
from tinygrad import Tensor, dtypes


class SimpleTextEncoder:
    """Lightweight text encoder with vocabulary embedding table for standalone testing."""

    def __init__(
        self,
        vocab_size: int = 32000,
        embed_dim: int = 1024,
        max_seq_len: int = 512,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.vocab_size = vocab_size
        self.embed_dim = embed_dim
        self.max_seq_len = max_seq_len
        self.device = device
        self.dtype = dtype

        emb_np = (np.random.randn(vocab_size, embed_dim) * 0.02).astype(np.float16)
        self.embed_tokens = Tensor(emb_np, device=device, dtype=dtype)

    def encode_tokens(self, token_ids: list[int]) -> Tensor:
        tokens = token_ids[: self.max_seq_len]
        if len(tokens) < self.max_seq_len:
            tokens = tokens + [0] * (self.max_seq_len - len(tokens))

        embeds_np = np.zeros((1, self.max_seq_len, self.embed_dim), dtype=np.float16)
        all_emb = self.embed_tokens.numpy()
        for i, tid in enumerate(tokens):
            tid = min(max(tid, 0), self.vocab_size - 1)
            embeds_np[0, i] = all_emb[tid]

        return Tensor(embeds_np, device=self.device, dtype=self.dtype)

    def encode_text(self, text: str) -> Tensor:
        words = text.lower().strip().split()
        token_ids = []
        for w in words:
            h = (
                sum(ord(c) * (31**i) for i, c in enumerate(w)) % (self.vocab_size - 1)
                + 1
            )
            token_ids.append(h)

        if not token_ids:
            token_ids = [0]

        return self.encode_tokens(token_ids)
