"""L2 -> L1 -> artifact: encode the tasks and write the dispatch schedule.

The only pass that calls the backend.
"""

from kohakuaccel.artifact import Artifact, Await, Barrier, Kick, SeedCredits
from kohakuaccel.backend.slots import EncodeContext
from kohakuaccel.ir.l1 import ProgramIR
from kohakuaccel.ir.l2 import ScheduleIR
from kohakuaccel.passes.manager import Context, Pass


class Emit(Pass):
    """Turn a placed, packed schedule into an L1 program and an artifact.

    Tasks in a round that land on the same unit are staged as ONE program, so a
    unit is kicked once per round however many tasks it was given.
    """

    name = "emit"

    def run(self, level: ScheduleIR, ctx: Context) -> ProgramIR:
        machine, backend = ctx.machine, ctx.backend
        peers = ctx.notes.get("coalesce.peers", {})
        leads = ctx.notes.get("coalesce.leads", {})

        out = ProgramIR(machine=machine.name)
        steps = []

        for r, rnd in enumerate(level.rounds):
            by_coord: dict[tuple[int, int], list[int]] = {}
            for i in rnd.tasks:
                by_coord.setdefault(level.placement[i], []).append(i)

            steps.append(SeedCredits(rnd.credits))
            waits = []

            for coord in sorted(by_coord):
                flits: list[int] = []
                signals = 0
                base = len(out.staging)
                for i in by_coord[coord]:
                    task = level.nodes[i]
                    got = backend.encode(
                        task,
                        EncodeContext(
                            coord=coord,
                            slot=base + len(flits),
                            peers=peers.get(i, ()),
                            leads=leads.get(i, True),
                        ),
                    )
                    if len(got) != task.flits:
                        raise ValueError(
                            f"task {task.described(i)} declared {task.flits} flits "
                            f"and its encoder produced {len(got)}; round packing "
                            f"and the staging layout were both computed from the "
                            f"declared number"
                        )
                    flits.extend(got)
                    signals += task.signals

                out.stage(coord, flits, signals, r, label=f"r{r}@{coord}")
                steps.append(Kick(coord, base, len(flits)))
                waits.append(Await(coord, signals))

            steps.extend(waits)
            if r != len(level.rounds) - 1:
                steps.append(Barrier())

        ctx.note(
            "artifact",
            Artifact(
                machine=machine.name,
                flits=list(out.staging),
                steps=steps,
                meta={
                    "rounds": len(level.rounds),
                    "tasks": len(level.nodes),
                    "coalesced_bytes": ctx.notes.get("coalesce.bytes_saved", 0),
                },
            ),
        )
        return out
