"""The plans `seam.to_program` made, by the key its PROGRAM's binary carries,
and the error a refused kernel raises."""

#: ast key -> `lower.Plan`
PLANS: dict = {}
#: Exact immutable AST witnesses for the binary-key transport namespace.
PLAN_ASTS: dict = {}


class KTPUUnsupported(RuntimeError):
    """A kernel refused by TG.PLAN.010, never computed some other way."""

    clause = "TG.PLAN.010"

    def __init__(self, message: str):
        super().__init__(f"{message} [{self.clause}]")


__all__ = ["PLANS", "PLAN_ASTS", "KTPUUnsupported"]
