"""A Streaming Row Unit's instructions: CFG, LOAD, RUN, STORE and POST, each
one payload word, encoded through `isa.sru`. `Config` is a job's
configuration as the CFG words that differ from what the unit holds."""

from dataclasses import dataclass

from kohakutpu.compiler.isa import sru as S


@dataclass(frozen=True)
class Config:
    """CFG words, ``((address, value), ...)`` in the order written."""

    words: tuple
    patches: tuple = ()

    def flits(self) -> list[int]:
        return [S.CFG.encode(word=a, value=v) for a, v in self.words]


@dataclass(frozen=True)
class Load:
    """`words` 32-byte words from `addr` into the table (4 entries a word),
    the column constants (2 a word) or the row state (a row a word), from
    entry `first`."""

    what: int
    addr: int
    words: int
    first: int = 0
    patches: tuple = ()

    def flits(self) -> list[int]:
        return [
            S.LOAD.encode(
                what=self.what, first=self.first, addr=self.addr, words=self.words
            )
        ]


@dataclass(frozen=True)
class Run:
    """One job over the configuration the unit holds. A from memory at
    `a` (sub-rows `astride` words apart) or, with `a` None, a COUNT stream
    whose producer `src` takes the credits; out to memory at `out` or to
    the unit `to` (buffer `buf`, granule `at`, `count`)."""

    a: int | None
    astride: int
    out: int = 0
    ostride: int = 0
    b: int = 0
    bstride: int = 0
    src: tuple = (0, 0)
    to: tuple | None = None
    buf: int = 0
    at: int = 0
    count: bool = False
    #: A stream's words (the job's NW x NSR): the credits its producer gets.
    words: int = 0
    patches: tuple = ()

    def flits(self) -> list[int]:
        peer = self.to is not None
        to = self.to or (0, 0)
        return [
            S.RUN.encode(
                amem=int(self.a is not None),
                peer=int(peer),
                aaddr=self.a or 0,
                astride=self.astride,
                oaddr=0 if peer else self.out,
                ostride=self.ostride,
                baddr=self.b,
                bstride=self.bstride,
                px=to[0],
                py=to[1],
                pbuf=self.buf,
                poff=self.at,
                count=int(self.count),
                ax=self.src[0],
                ay=self.src[1],
                words=self.words if self.a is None else 0,
            )
        ]


@dataclass(frozen=True)
class Store:
    """Row state rows `first`.. (`rows` of them) to `addr`, a row a word."""

    addr: int
    rows: int
    first: int = 0
    patches: tuple = ()

    def flits(self) -> list[int]:
        return [S.STORE.encode(first=self.first, addr=self.addr, rows=self.rows)]


@dataclass(frozen=True)
class Post:
    """`n` credits (32-byte granules) to the unit at `dst`."""

    dst: tuple
    n: int
    patches: tuple = ()

    def flits(self) -> list[int]:
        return [S.POST.encode(x=self.dst[0], y=self.dst[1], n=self.n)]


__all__ = ["Config", "Load", "Post", "Run", "Store"]
