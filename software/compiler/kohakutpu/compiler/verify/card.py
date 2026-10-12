"""The card the processes share: units by coordinate, the nodes that feed
them, and the routes between them -- credits, bursts, acknowledgements and
shared fills.

A burst lands as one event of its receiver's port (`model.Port`): it counts
toward the receiver's counters with that event's clock, and the
DATA_RECEIVED of a burst the node sent counts as one completion of the
receiver in that node's dispatch state (`engine.c` ka_engine_drain).
"""

from kohakutpu.compiler.verify.cluster import Cluster, burst_fields
from kohakutpu.compiler.verify.model import Clock, Machine
from kohakutpu.compiler.verify.vector import Vector

KINDS = {"MG": Cluster, "VC": Vector}


class Card:
    def __init__(self, m: Machine, machine, trips: int = 3) -> None:
        self.m, self.machine = m, machine
        #: times a node loop on data is run (verify.md s5)
        self.trips = trips
        self.units: dict = {}
        self.nodes: dict = {}  # mesh -> Node
        #: unit -> the node whose package sends it words
        self.feeder: dict = {}
        #: (mesh, ("bell", from) | ("slot", n)) -> doorbells / amount consumed
        self.taken: dict = {}
        #: the keys of `taken` some wait reads
        self.waited: set = set()
        #: shared fills: (mesh, sharers) -> {sharer: [arrival clocks]}
        self.shares: dict = {}
        #: (sequencer, pc) -> (group, the arrival's index)
        self.arrived: dict = {}
        #: per non-leading sharer: (unit, group, lead, k, own arrival, own proc)
        self.windows: list = []
        #: DATA_RECEIVED counted by a node: (node, receiver, send clock, landing clock)
        self.acks: list = []

    # ------------------------------------------------------------ the units
    def unit(self, key: tuple):
        """The unit at (mesh, x, y), made when first named; None if the
        machine has nothing there."""
        u = self.units.get(key)
        if u is not None:
            return u
        mesh, x, y = key
        for kind, coords in self.machine.mesh(mesh).units.items():
            if (x, y) in coords and kind in KINDS:
                u = self.units[key] = KINDS[kind](
                    self, key, f"{kind.lower()}({mesh}:{x},{y})"
                )
                return u
        return None

    def feed(self, node, u) -> None:
        self.feeder[u] = node

    def node_done(self, u) -> bool:
        node = self.feeder.get(u)
        return node is None or node.done()

    def ports(self, mesh: int) -> tuple:
        return tuple(tuple(p) for p in self.machine.mesh(mesh).mem_ports)

    # ------------------------------------------------------------ the routes
    def post(self, u, to: tuple, n: int, clock: Clock, owner: int) -> None:
        dst = self.unit((u.key[0], *to))
        if dst is None:
            self.m.find("route", u.name, f"a credit of {n} to {to}, where no unit is")
            return
        dst.credit(n, clock, owner)

    def burst(self, u, f: dict, off: int, n: int, clock: Clock, owner: int) -> None:
        self.send(u, burst_fields(f), off, n, clock, owner)

    def send(self, u, sink: dict, off: int, n: int, clock: Clock, owner: int) -> None:
        """A CU_DATA burst of `n` granules from `u` to `sink` (`to`, `far`,
        `mesh`, `buf`, `count`, `signal`, `ack`)."""
        mesh = sink["mesh"] if sink["far"] else u.key[0]
        to = sink["far"] or sink["to"]
        if to == (0, 0) or to in self.ports(mesh):
            # The node's RX queue: the descriptor and every word, a flit each.
            node = self.nodes.get(mesh)
            if node is not None:
                node.rx.add(n + 1, clock, owner)
            return
        dst = self.unit((mesh, *to))
        if dst is None:
            self.m.find(
                "route", u.name, f"a burst of {n} to {mesh}:{to}, where no unit is"
            )
            return
        landed = dst.port.tick(clock, *dst.joins(sink["buf"]))
        dst.landings.append((owner, clock.copy(), landed))
        why = dst.land(
            sink["buf"], off, n, sink["count"], landed, f"a burst from {u.name}", clock
        )
        if why:
            self.m.find("bounds", u.name, f"burst to {dst.name} rejected: {why}")
        if sink["signal"]:
            # An ack field of 0 means the burst's sender; a nonzero one names a
            # unit coordinate, and the node's processor has none (H2): only a
            # burst the node sent itself is acknowledged to the node.
            node = self.nodes.get(mesh)
            if sink["ack"] == (0, 0) and u is node and self.feeder.get(dst) is node:
                dst.recv.add(1, landed, dst.port.ix)
                dst.acks.add(1, landed, dst.port.ix)
                self.acks.append((node, dst, clock.copy(), landed))

    # ------------------------------------------------------------ shared fills
    def rendezvous(self, seq, f: dict):
        """``(released, clock)`` for a shared FILL: its read is issued by the
        lowest-indexed sharer and lands in every sharer, so each sharer's k-th
        fill of the group completes once every sharer reached its k-th."""
        u = seq.u
        mesh = u.key[0]
        me = (u.key[2] << 4) | u.key[1]
        others = [(f["peers"] >> (8 * i)) & 0xFF for i in range(f["npeer"])]
        group = (mesh, tuple(sorted({me, *others})))
        arrivals = self.shares.setdefault(group, {})
        key = (seq, seq.pc)
        if key not in self.arrived:
            mine = arrivals.setdefault(me, [])
            mine.append(seq.tick())
            self.arrived[key] = len(mine)
            lead = min(group[1])
            if me != lead:
                self.windows.append((u, group, lead, len(mine) - 1, mine[-1], seq.ix))
        k = self.arrived[key]
        if any(len(arrivals.get(s, ())) < k for s in group[1]):
            return False, None, None
        out = Clock()
        for s in group[1]:
            out.join(arrivals[s][k - 1])
        return True, out, arrivals[min(group[1])][k - 1]

    def lead_arrival(self, group: tuple, lead: int, k: int):
        """The lead sharer's k-th arrival clock and process, if it arrived."""
        got = self.shares.get(group, {}).get(lead, ())
        if k >= len(got):
            return None, None
        mesh = group[0]
        u = self.units.get((mesh, lead & 0xF, lead >> 4))
        return got[k], (u.seq.ix if u is not None else None)
