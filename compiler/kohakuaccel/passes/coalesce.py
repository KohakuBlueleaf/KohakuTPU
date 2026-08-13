"""Merging fetches that several tasks in one round would each make.

A read request carries extra destinations, so one fetch can serve several units.
Opt-in by the backend: this records who could share with whom, and an encoder
using `peers` must also honour `leads` or the followers receive the data twice.
"""

from kohakuaccel.ir.l2 import ScheduleIR
from kohakuaccel.passes.manager import Context, Pass


class Coalesce(Pass):
    """Record, per round, which tasks read an identical region.

    Leaves ``coalesce.peers`` and ``coalesce.leads`` in the context: the first
    maps a leader task to the other coordinates its fetch could serve, the second
    marks which tasks still issue their own fetch.

    Sharers are grouped per mesh as well as per region: a fetch's extra
    destinations are coordinates, which name a unit only within one mesh.
    """

    name = "coalesce"

    def run(self, level: ScheduleIR, ctx: Context) -> ScheduleIR:
        peers: dict[int, tuple[tuple[int, int], ...]] = {}
        leads: dict[int, bool] = {i: True for i in range(len(level.nodes))}
        saved = 0

        for rnd in level.rounds:
            by_region: dict[object, list[int]] = {}
            for i in rnd.tasks:
                for region in level.nodes[i].reads:
                    by_region.setdefault((region, level.meshes[i]), []).append(i)

            for (region, _), sharers in by_region.items():
                coords = {level.placement[i] for i in sharers}
                if len(sharers) < 2 or len(coords) < 2:
                    continue
                leader = sharers[0]
                followers = tuple(
                    level.placement[i]
                    for i in sharers[1:]
                    if level.placement[i] != level.placement[leader]
                )
                if not followers:
                    continue
                peers[leader] = peers.get(leader, ()) + followers
                for i in sharers[1:]:
                    leads[i] = False
                saved += len(followers) * region.nbytes

        ctx.note("coalesce.peers", peers)
        ctx.note("coalesce.leads", leads)
        ctx.note("coalesce.bytes_saved", saved)
        return level
