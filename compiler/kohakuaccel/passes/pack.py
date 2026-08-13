"""Cutting a schedule into dispatch rounds under three machine bounds.

The staging RAM holds `stage_flits` flits, the command RAM `ncmd` commands, and
a unit's instruction FIFO `inst_depth` instructions. The third is a correctness
bound: an overfilled FIFO backpressures the link carrying the memory responses
that unit waits on, so it wedges rather than slows.
"""

from kohakuaccel.ir.l2 import Round, ScheduleIR
from kohakuaccel.passes.manager import Context, Pass

# A kick costs a poll, up to three latched writes and the launch write; each
# wait costs one more, and a round pays one seed plus a final halt.
COMMANDS_PER_KICK = 5
COMMANDS_PER_AWAIT = 1
COMMANDS_PER_ROUND = 2


class Pack(Pass):
    """Group tasks into rounds under the staging, command and credit bounds.

    A task joins the open round only if every task it depends on is in an EARLIER
    round, so a round is internally independent and one barrier closes it.
    """

    name = "pack"

    def run(self, level: ScheduleIR, ctx: Context) -> ScheduleIR:
        machine = ctx.machine
        order = level.order()
        preds = level.predecessors()
        placed = {i: level.placement[i] for i in range(len(level.nodes))}

        rounds: list[Round] = []
        done: set[int] = set()
        pending = list(order)

        while pending:
            current = Round(credits=machine.inst_depth)
            flits = 0
            commands = COMMANDS_PER_ROUND
            per_unit: dict[tuple[int, int], int] = {}
            taken: list[int] = []

            for i in pending:
                task = level.nodes[i]
                if not preds[i] <= done:
                    continue
                coord = placed[i]
                if flits + task.flits > machine.stage_flits:
                    continue
                if commands + COMMANDS_PER_KICK + COMMANDS_PER_AWAIT > machine.ncmd:
                    continue
                if per_unit.get(coord, 0) + task.flits > machine.inst_depth:
                    continue

                taken.append(i)
                flits += task.flits
                commands += COMMANDS_PER_KICK + COMMANDS_PER_AWAIT
                per_unit[coord] = per_unit.get(coord, 0) + task.flits

            if not taken:
                blocked = pending[0]
                raise ValueError(
                    f"task {level.nodes[blocked].described(blocked)} fits in no "
                    f"round: it needs {level.nodes[blocked].flits} flits against a "
                    f"staging RAM of {machine.stage_flits}"
                )

            current.tasks = taken
            rounds.append(current)
            done.update(taken)
            pending = [i for i in pending if i not in done]

        level.rounds = rounds
        ctx.note("pack.rounds", len(rounds))
        return level
