"""The plans `seam.to_program` made, by the key its PROGRAM's binary carries,
and the error a refused kernel raises."""

#: ast key -> `lower.Plan`
PLANS: dict = {}


class KTPUUnsupported(RuntimeError):
    """A kernel KTPU refuses, by name (never computed some other way)."""


__all__ = ["PLANS", "KTPUUnsupported"]
