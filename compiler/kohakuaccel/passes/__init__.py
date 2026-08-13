"""Framework passes, and the manager that verifies between them."""

from kohakuaccel.passes.coalesce import Coalesce
from kohakuaccel.passes.emit import Emit
from kohakuaccel.passes.manager import Context, Pass, PassManager, PassRecord
from kohakuaccel.passes.pack import Pack
from kohakuaccel.passes.place import Place

__all__ = [
    "Coalesce",
    "Context",
    "Emit",
    "Pack",
    "Pass",
    "PassManager",
    "PassRecord",
    "Place",
]
