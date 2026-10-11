"""The errors this vocabulary raises."""


class LangError(ValueError):
    """A kernel statement this machine cannot express, and why."""


class CannotFuse(LangError):
    """A fused epilogue this machine cannot run, for a reason RETILING fixes.

    Distinguished from `LangError` so `compile` can stage the kernel instead of
    handing the author a refusal: whether a drain reaches a vector core depends
    on the machine and the shape, neither of which the trace knows. Raised only
    where the unfused form would succeed -- never for an expression that is
    wrong however it is scheduled.
    """


class CutsRows(LangError):
    """An instance of a stage that folds rows would start part way through one.

    `cols` is the row width; an instance `part=` of whole rows fixes it, which
    the compiler retries with rather than refusing.
    """

    def __init__(self, message: str, cols: int) -> None:
        super().__init__(message)
        self.cols = cols
