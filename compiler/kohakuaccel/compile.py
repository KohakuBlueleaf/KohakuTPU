"""The default pipeline: a schedule in, an artifact out."""

from dataclasses import dataclass

from kohakuaccel.artifact import Artifact
from kohakuaccel.ir.l1 import ProgramIR
from kohakuaccel.ir.l2 import ScheduleIR
from kohakuaccel.passes.coalesce import Coalesce
from kohakuaccel.passes.emit import Emit
from kohakuaccel.passes.manager import Context, PassManager
from kohakuaccel.passes.pack import Pack
from kohakuaccel.passes.place import Place


def default_passes():
    """Place, pack, coalesce, emit -- the order their inputs require."""
    return [Place(), Pack(), Coalesce(), Emit()]


@dataclass
class Result:
    """Everything a compile produced: artifact, program, context and manager."""

    artifact: Artifact
    program: ProgramIR
    context: Context
    manager: PassManager

    def report(self) -> str:
        lines = [self.artifact.summary(), self.manager.report()]
        saved = self.context.notes.get("coalesce.bytes_saved", 0)
        if saved:
            lines.append(f"coalescing saved {saved} bytes of fetch")
        return "\n".join(lines)


def compile(schedule: ScheduleIR, machine, backend, passes=None, verify=True) -> Result:
    """Run the pipeline over `schedule`, inferring memory edges first."""
    schedule.infer_edges()
    schedule.verify()

    ctx = Context(machine=machine, backend=backend)
    manager = PassManager(passes if passes is not None else default_passes(), verify)
    program = manager.run(schedule, ctx)
    return Result(ctx.notes["artifact"], program, ctx, manager)
