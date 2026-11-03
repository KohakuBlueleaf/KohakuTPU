"""The ISSUES 79 executing-writer stamp and its classifier. A stamped word is a tag
word (`check.tags`) whose four f16 lanes a writer multiplied by 2^DELTAS[w] between
fill and drain: each lane's exponent k + 1 becomes k + 1 + DELTAS[w] (16..28,
finite and exact), so a word names its producer, offset, epoch and the writer that
transformed it. Two stamps overflow a lane rather than alias a third writer.

`classify` decodes every retained mismatch from its raw got / want words, never
from `check.decode`; a word whose lanes disagree on the stamp stays unknown.
The cut it names is an inference about where foreign data entered, not a gate."""

import collections
import dataclasses

import numpy as np

from h import check

#: Writer w's exponent shift.
DELTAS = (15, 18, 21, 24)


def stamp(writer: int) -> int:
    """What writer `writer`'s transform adds to a tag word."""
    return sum((DELTAS[writer] << 10) << (16 * k) for k in range(4))


def decode(word: int) -> dict | None:
    """A stamped or plain tag word's fields (`writer` None: unstamped); None for a
    word whose lanes carry different shifts or a shift no writer makes."""
    lanes = [(word >> (16 * k)) & 0xFFFF for k in range(4)]
    shifts = {(lane >> 10) - (k + 1) for k, lane in enumerate(lanes)}
    if len(shifts) != 1 or (shift := shifts.pop()) not in (0, *DELTAS):
        return None
    f = [lane & check.FIELD for lane in lanes]
    return dict(
        writer=DELTAS.index(shift) if shift else None,
        producer=f[3] >> 6,
        region=f[3] & 63,
        offset=f[0] | f[1] << 10 | (f[3] & 63) << 20,
        epoch=f[2],
    )


@dataclasses.dataclass(frozen=True)
class Gather:
    """P12's all-gather geometry: writer q (sysnode `sysnodes[q]`) owns columns
    q * width.. of every y row (pitch = writers * width); reader r's v holds the
    last `tile` bytes of its `rows / readers` rows of y."""

    sysnodes: tuple[int, ...]
    rows: int
    width: int
    readers: int
    tile: int
    stamped: bool

    @property
    def pitch(self) -> int:
        return len(self.sysnodes) * self.width

    def y_of(self, output: str, at: int) -> int:
        """The y byte offset whose value output byte `at` holds."""
        if output == "y":
            return at
        reader, local = divmod(at, self.tile)
        share = self.rows // self.readers * self.pitch
        return (reader + 1) * share - self.tile + local

    def kind(self, writer: int, got: dict | None) -> str:
        """Where foreign data entered, given the intended writer and the got tag."""
        if got is None:
            return "invalid_or_mixed"
        if not self.stamped:
            return "unstamped" if got["writer"] is None else "stamp_in_control"
        if got["writer"] is None:
            return "raw_input_or_missing_transform"
        if got["writer"] == writer:
            return "before_victim_transform"
        if self.sysnodes[got["writer"]] == got["producer"]:
            return "after_donor_transform"
        return "other_writer"

    def word(self, output: str, binds: dict, epoch: int, item) -> dict:
        """One retained mismatch ``[word, got, want]`` decoded and classified."""
        at, raw_got, raw_want = item
        yat = self.y_of(output, 8 * at)
        row, within = divmod(yat, self.pitch)
        writer, col = divmod(within, self.width)
        offset = (row * self.width + col) // 8
        got = decode(raw_got)
        out = dict(
            at=at,
            address=binds[output] + 8 * at,
            y_address=binds["y"] + yat,
            writer=writer,
            producer=self.sysnodes[writer],
            offset=offset,
            got=got,
            want=decode(raw_want),
            kind=self.kind(writer, got),
            raw=[f"{raw_got:#018x}", f"{raw_want:#018x}"],
        )
        if got is not None and got["producer"] in self.sysnodes:
            q = self.sysnodes.index(got["producer"])
            src_row, src_col = divmod(8 * got["offset"], self.width)
            out |= dict(
                producer_ok=got["producer"] == self.sysnodes[writer],
                offset_ok=got["offset"] == offset,
                epoch_ok=got["epoch"] == epoch % check.EPOCHS,
                origin_x=binds["x"] + q * self.rows * self.width + 8 * got["offset"],
                origin_y=binds["y"] + src_row * self.pitch + q * self.width + src_col,
            )
        return out

    def __call__(self, outputs: dict, binds: dict, epoch: int) -> dict:
        """Every retained mismatch of each output's first read and of its quiet
        reread, classified, with each read's per-kind counts and hidden words."""
        out = {}
        for p, o in outputs.items():
            if not o.get("mismatched"):
                continue
            reads = {"first": o} | ({"reread": o["reread"]} if "reread" in o else {})
            out[p] = {}
            for read, r in reads.items():
                items = [self.word(p, binds, epoch, b) for b in r["bad"]]
                out[p][read] = dict(
                    mismatched=r["mismatched"],
                    hidden=r["mismatched"] - len(items),
                    kinds=dict(collections.Counter(w["kind"] for w in items)),
                    words=items,
                )
        return out


def stamped(x: np.ndarray, writers: int) -> np.ndarray:
    """`x` (u64 words, writer-major) with writer q's stamp added to its slice."""
    deltas = np.array([stamp(q) for q in range(writers)], np.uint64)
    return (x.reshape(writers, -1) + deltas[:, None]).reshape(-1)
