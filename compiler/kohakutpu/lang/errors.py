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
