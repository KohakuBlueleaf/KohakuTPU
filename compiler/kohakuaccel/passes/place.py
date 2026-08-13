"""Assigning tasks to a mesh, and to a coordinate on it."""

from kohakuaccel.ir.l2 import Policy, ScheduleIR, Task
from kohakuaccel.passes.manager import Context, Pass


class Place(Pass):
    """Give every task a mesh and a coordinate.

    The mesh is a CONSTRAINT -- a task runs where its operands are -- so only
    the coordinate within it is a scheduling choice.

    PINNED takes the task's own and checks it against the machine. FREE
    balances accumulated cost across units of the right type. AFFINE prefers
    the unit fewest hops from the memory port serving it, breaking ties on
    load, which is accumulated per (mesh, coordinate).

    Leaves ``place.load`` and ``place.remote``, the second naming the tasks
    touching memory off their own mesh.
    """

    name = "place"

    def run(self, level: ScheduleIR, ctx: Context) -> ScheduleIR:
        machine, backend = ctx.machine, ctx.backend
        load: dict[tuple[int, tuple[int, int]], float] = {}
        placement, meshes, remote = [], [], {}

        for i, task in enumerate(level.nodes):
            mesh = _resolve_mesh(machine, task, task.described(i))
            candidates = machine.coords(task.unit_type, mesh)
            policy = backend.policy(task)

            if policy is Policy.PINNED:
                if task.coord not in candidates:
                    raise ValueError(
                        f"task {task.described(i)} is pinned to {task.coord}, "
                        f"which carries no {task.unit_type!r} unit on mesh_{mesh}; "
                        f"they are at {list(candidates)}"
                    )
                chosen = task.coord
            elif policy is Policy.AFFINE and machine.mesh(mesh).mem_ports:
                chosen = min(
                    candidates,
                    key=lambda c: (
                        _port_hops(machine, c, mesh),
                        load.get((mesh, c), 0.0),
                        c,
                    ),
                )
            else:
                chosen = min(candidates, key=lambda c: (load.get((mesh, c), 0.0), c))

            load[(mesh, chosen)] = load.get((mesh, chosen), 0.0) + backend.cost(task)
            placement.append(chosen)
            meshes.append(mesh)
            off = _off_mesh(task, mesh)
            if off:
                remote[i] = off

        level.placement = placement
        level.meshes = meshes
        ctx.note("place.load", load)
        ctx.note("place.remote", remote)
        return level


def _resolve_mesh(machine, task: Task, described: str) -> int:
    """The mesh a task runs on: its own, else its operands', else the default.

    Reads decide before writes, because a unit fetches operands through its own
    mesh's memory agent while a result may be addressed elsewhere.

    Raises :class:`ValueError` if the task reads several meshes and names none,
    since there is then no basis to choose one.
    """
    if len(machine.meshes) == 1:
        return machine.meshes[0].index
    if task.mesh is not None:
        return task.mesh
    for regions in (task.reads, task.writes):
        found = {r.mesh for r in regions}
        if len(found) == 1:
            return found.pop()
        if found:
            raise ValueError(
                f"task {described} reads meshes {sorted(found)} and names none "
                f"to run on; set Task.mesh, or move the operands together"
            )
    return machine.default


def _off_mesh(task: Task, mesh: int) -> tuple[int, ...]:
    """Meshes other than `mesh` that this task's regions touch, in order."""
    return tuple(sorted({r.mesh for r in task.reads + task.writes} - {mesh}))


def _port_hops(machine, coord, mesh) -> int:
    port = machine.nearest_port(coord, mesh)
    return 0 if port is None else machine.hops(coord, port)
