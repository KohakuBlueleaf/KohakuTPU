"""A whole hardware session against a fake machine, with no card anywhere.

The fake answers the four registers a run actually depends on -- A_CAPS,
A_PROG_STAT, A_SIG_DONE and NODE_STATUS -- and stores everything written. That
is enough to drive `Session.run` end to end, which is what these tests do:
handshake, reset, operand upload, every round executed by the host, result
readback, and a scored payload out the other side.

It cannot check the ANSWER -- no arithmetic happens -- so what is checked is
everything else: that the bytes land at board addresses, that the payload is
the simulator's shape, and that each way the machine can fail to answer is
reported as itself rather than as a wrong number.
"""

import numpy as np
import pytest

from ktpu.hw import bench, fpga, kernel
from ktpu.hw import board as bd
from ktpu.hw import device as dev

BOARD = "singlemesh_2x2"


class FakeMachine(dev.MemoryTransport):
    """A card that accepts everything and completes instantly.

    Absolute addresses: it sits UNDER the board's rebasing, so every address it
    sees is one the real card would see -- which is what makes the assertions
    about the ctrl window mean anything.

    A KICK retires its whole program at once, adding PROG_LEN completions to
    SIG_DONE and to the destination node, because that is what the real machine
    eventually does and it is what the control program polls for. A fake that
    answered a constant would pass whatever the driver asked, including a
    driver that waited for the wrong number.
    """

    def __init__(self, board, caps=None, sig=None, busy=0, stall=False):
        super().__init__()
        self.board = board
        self.busy = busy
        self.sig = sig
        # A kick that is accepted and never reported -- the actual hang. `sig`
        # only pins the SHARED counter, so it stopped modelling a stall the
        # moment a round could be awaited per cluster instead.
        self.stall = stall
        self.done = 0
        self.nodes = {}
        self.kicks = []
        self.rx = []
        # What the CU_CTRL registers answer. `ctr` and `dbg` are per node so a
        # test can give two nodes different histories, which is the whole point
        # of reading them per node.
        self.version = 1
        self.inst_space = 32
        self.ctr = {}
        self.dbg = {}
        self.nodes_present = {
            **{tuple(c): dev.CU_MATMUL for c in board.clusters},
            **{tuple(v): dev.CU_VECTOR for v in board.vectors},
        }
        grid = max(board.ncol, board.nrow)
        self.caps = (grid << 32) | (1 << 24) | (4 << 16) | 288 if caps is None else caps
        self.on_read = {
            board.ctrl(dev.A_CAPS): lambda n: self.caps,
            board.ctrl(dev.A_PROG_STAT): self._stat,
            board.ctrl(dev.A_SIG_DONE): lambda n: self._sig(),
            board.ctrl(dev.A_RX_STATUS): lambda n: 0 if self.rx else (1 << 16),
        }
        for w in range(dev.FLIT_WORDS):
            self.on_read[board.ctrl(dev.A_RX_FLIT0 + w * 8)] = lambda n, i=w: (
                (self.rx[0] >> (i * 64)) & dev.MASK64 if self.rx else 0
            )
        self.node_code = {}
        for x, y in (*board.clusters, *board.vectors):
            idx = dev.node_index(x, y)
            self.on_read[board.ctrl(dev.A_NODE + idx * 8)] = (
                lambda n, i=idx: self._node_status(i)
            )

    def _node_status(self, idx):
        """`{code[8], arg[32], count[16], 7'd0, valid}`, as the orchestrator packs it."""
        code, arg = self.node_code.get(idx, (0, 0))
        return (
            (code << 56)
            | ((arg & dev.MASK32) << 24)
            | ((self.nodes.get(idx, 0) & 0xFFFF) << 8)
            | 1
        )

    def _staged(self, slot):
        flit = 0
        for w in range(dev.FLIT_WORDS):
            addr = self.board.ctrl(dev.A_STAGE + (slot * dev.FLIT_WORDS + w) * 8)
            flit |= self.mem.get(addr, 0) << (w * 64)
        return flit

    def _retire(self, dst, base, nflits):
        """One signal per CU_INST; a whole CU_DATA burst answers exactly once.

        Modelled from the type of the staged flit, because a fake that retired
        per flit either way would pass a driver that waited for the wrong count
        -- which is the only thing the CU_DATA wait can get wrong.
        """
        if self.stall:
            return
        if dev._field(self._staged(base), 271, 4) == dev.T_CU_DATA:
            buf_id = dev._field(self._staged(base), 255, 8)
            self.node_code[dst] = (dev.SIG_DATA_RECEIVED, buf_id)
            nflits = 1
        else:
            self.node_code[dst] = (dev.SIG_INST_COMPLETE, 0)
        self.nodes[dst] = self.nodes.get(dst, 0) + nflits
        self.done += nflits

    def write64(self, addr, data):
        super().write64(addr, data)
        if addr == self.board.ctrl(dev.A_TX_KICK):
            self._mailbox(addr, data)
        elif addr == self.board.ctrl(dev.A_RX_POP):
            if self.rx:
                self.rx.pop(0)
        elif addr == self.board.ctrl(dev.A_PROG_KICK):
            dst = self.mem.get(self.board.ctrl(dev.A_PROG_DST), 0)
            nflits = self.mem.get(self.board.ctrl(dev.A_PROG_LEN), 0)
            base = self.mem.get(self.board.ctrl(dev.A_PROG_BASE), 0)
            self.kicks.append((dst, nflits))
            self._retire(dst, base, nflits)
        elif addr == self.board.ctrl(dev.A_SIG_DONE):
            # ONLY the global counter. `node_status` in noc_orchestrator.v is
            # written on every inbound CU_SIGNAL and cleared by NOTHING -- not
            # by this write, not by resetn; the block has no reset clause at
            # all. Clearing it here would certify an absolute per-node wait
            # that hangs on the real card from the second round onward.
            self.done = 0

    def _mailbox(self, addr, data):
        """Answer a CU_CTRL read the way noc_cu_base does: reply to the source.

        Modelled rather than stubbed, because the thing under test is the flit
        ENCODING -- a stub that echoed a canned reply would pass with the
        header fields in any order at all.
        """
        req = 0
        for w in range(dev.FLIT_WORDS):
            req |= self.mem.get(self.board.ctrl(dev.A_TX_FLIT0 + w * 8), 0) << (w * 64)
        dst = (dev._field(req, 287, 4), dev._field(req, 283, 4))
        src = (dev._field(req, 279, 4), dev._field(req, 275, 4))
        if dst == tuple(self.board.agent):
            # Port 0 hands a non-memory flit to the agent, so the REQUEST comes
            # back. Real, and it reported a phantom node until the opcode check.
            self.rx.append(req)
            return
        if dst not in self.nodes_present:
            return  # nothing there; the mailbox simply stays empty
        caps = self._ctrl_value(dst, dev._field(req, 247, 8))
        parts = [
            (src[0], 4),
            (src[1], 4),
            (dst[0], 4),
            (dst[1], 4),
            (dev.T_CU_CTRL, 4),
            (dev._field(req, 267, 8), 8),
            (1, 1),
            (0, 3),
            (dev.CTRL_READ_RESP, 8),
            (dev._field(req, 247, 8), 8),
            (caps, 64),
        ]
        flit, at = 0, dev.FLIT_BITS
        for value, width in parts:
            at -= width
            flit |= value << at
        self.rx.append(flit)

    def _ctrl_value(self, node, idx):
        """The four control registers, laid out as `noc_cu_base` packs them."""
        kind = self.nodes_present[node]
        if idx == dev.CU_STATUS:
            return (self.inst_space & 0xFFFF) << 32
        if idx == dev.CU_COUNTERS:
            retired, busy = self.ctr.get(node, (0, 0))
            return ((retired & dev.MASK32) << 32) | (busy & dev.MASK32)
        if idx == dev.CU_DBG:
            return self.dbg.get(node, 0)
        return (kind << 48) | (self.version << 40) | (2 << 36) | (32 << 20)

    def _stat(self, n):
        """Busy for the first `busy` reads, so a reset can be made to wait."""
        return 1 if n <= self.busy else 0

    def _sig(self):
        return self.done if self.sig is None else self.sig


def session(**kw):
    board = bd.Board.named(BOARD)
    machine = FakeMachine(board, **kw)
    return fpga.Session(board=board, transport=machine, timeout=2.0), machine


def _flits(m, k, n):
    """The passes the planner produces for this shape on the shipped board."""
    with fpga._layout("split"):
        return bench.build(m, k, n, ncl=2, preq=(False, False)).kern.passes


# ---- handshake ----------------------------------------------------------
def test_the_handshake_decodes_the_capability_word():
    s, _ = session()
    c = s.handshake()
    assert c["flit_width"] == 288 and c["pos_width"] == 4
    assert c["grid_lo"] == 1 and c["grid_hi"] == 2


@pytest.mark.parametrize("caps", [0, (1 << 64) - 1, 0x1234])
def test_a_machine_that_is_not_there_is_named_as_such(caps):
    """0 and all-ones are what an absent slave returns; neither is an answer."""
    s, _ = session(caps=caps)
    with pytest.raises(fpga.HardwareError, match="Nothing is answering"):
        s.handshake()


def test_a_board_pointed_at_the_wrong_bitstream_is_caught():
    """The mistake that would put flits on nodes that do not exist."""
    s, _ = session(caps=(4 << 32) | (1 << 24) | (4 << 16) | 288)
    with pytest.raises(fpga.HardwareError, match="does not match the bitstream"):
        s.handshake()


# ---- reset --------------------------------------------------------------
def test_reset_clears_the_counter_and_waits_for_the_dispatcher():
    s, m = session(busy=3)
    s.reset()
    assert m.mem[s.board.ctrl(dev.A_SIG_DONE)] == 0
    assert m.reads[s.board.ctrl(dev.A_PROG_STAT)] == 4


def test_a_dispatcher_that_never_idles_says_there_is_no_soft_reset():
    s, _ = session(busy=10**9)
    with pytest.raises(fpga.HardwareError, match="reload the bitstream"):
        s.reset()


# ---- the mailbox --------------------------------------------------------
def test_a_ctrl_request_lays_the_header_out_the_way_the_rtl_reads_it():
    """noc_cu_base's HDR_* macros count DOWN from the MSB. Off by one field and
    the flit routes somewhere else entirely, which looks like a dead node."""
    f = dev.ctrl_request((3, 2), (0, 1), dev.CU_CAPS, txn=7)
    assert (dev._field(f, 287, 4), dev._field(f, 283, 4)) == (3, 2)
    assert (dev._field(f, 279, 4), dev._field(f, 275, 4)) == (0, 1)
    assert dev._field(f, 271, 4) == dev.T_CU_CTRL
    assert dev._field(f, 267, 8) == 7
    assert dev._field(f, 247, 8) == dev.CU_CAPS
    assert f < (1 << dev.FLIT_BITS)


def test_caps_decodes_the_fields_the_synthesis_log_reported():
    """'MG' v1, 2 buffers, INST_DEPTH 32 -- mx_cluster_cu's noc_cu_base."""
    caps = (dev.CU_MATMUL << 48) | (1 << 40) | (2 << 36) | (32 << 20)
    assert dev.decode_caps(caps) == {
        "type": dev.CU_MATMUL,
        "name": "MG",
        "version": 1,
        "buffers": 2,
        "inst_depth": 32,
    }


def test_every_node_the_board_claims_answers_with_its_own_type():
    s, _ = session()
    found = s.enumerate_nodes()
    assert len(found) == len(s.board.clusters) + len(s.board.vectors)
    for c in s.board.clusters:
        assert found[f"{c[0]},{c[1]}"]["name"] == "MG"
    for v in s.board.vectors:
        assert found[f"{v[0]},{v[1]}"]["name"] == "VC"
    assert s.check_nodes() == []


def test_a_bitstream_the_board_file_does_not_describe_is_caught():
    """The reason CU_VERSION was bumped to 0x02 mesh-wide.

    The shipped card is v1 and the RTL is now v2, so a board file transcribed
    for this bitstream must FAIL against the next one rather than pass quietly
    while its capacities and ISA have moved underneath.
    """
    s, m = session()
    for node in list(m.nodes_present):
        m.nodes_present[node] = m.nodes_present[node]
    real = m._mailbox

    def newer(addr, data):
        real(addr, data)
        m.rx[-1] += 1 << (176 + 40)  # CU_VERSION 1 -> 2 inside the caps word

    m._mailbox = newer
    why = s.check_nodes()
    assert why and all("DIFFERENT BITSTREAM" in w for w in why)
    assert "CU_VERSION 2" in why[0] and "written for 1" in why[0]
    # Bring-up reads this message before it reads the code. It has to say that
    # a refusal here is the gate working, or a stale card looks like a fault.
    assert "VERSION GATE DOING ITS JOB" in why[0]
    assert "gen_board.py" in why[0]


def test_the_agents_own_coordinate_is_an_echo_and_not_an_endpoint():
    """A real bug, found by sweeping the grid on silicon.

    Addressing the agent delivers the request back to its own receive queue.
    Source, destination and transaction tag are all ours, so every check except
    the OPCODE agrees -- and `sweep` duly reported a phantom node at (0,1)
    answering with an all-zero CU_CAPS.
    """
    s, _ = session()
    with pytest.raises(fpga.HardwareError, match="not a read response"):
        s.ctrl_read(s.board.agent, dev.CU_CAPS, timeout=2.0)


def test_a_full_grid_sweep_finds_the_board_file_complete_in_both_directions():
    """`enumerate_nodes` can only confirm what the file claims. This is the
    other direction -- nothing answers that the file does not mention."""
    s, _ = session()
    r = s.sweep(timeout=0.2)
    assert r["undeclared"] == [] and r["missing"] == []
    assert len(r["answered"]) == len(s.board.clusters) + len(s.board.vectors)


def test_a_node_that_is_not_there_is_reported_and_does_not_hang():
    s, m = session()
    m.nodes_present.pop((1, 2))
    assert "did not answer" in " ".join(s.check_nodes())
    assert "error" in s.enumerate_nodes()["1,2"]


def test_a_stale_reply_cannot_answer_the_next_question():
    """The mailbox is drained before asking, or question N reads answer N-1."""
    s, m = session()
    m.rx.append(dev.ctrl_request((9, 9), (0, 1), 0, txn=200))
    caps = s.ctrl_read((1, 1), dev.CU_CAPS, timeout=2.0)
    assert dev.decode_caps(caps)["name"] == "MG"


def test_the_reply_must_carry_the_tag_that_was_sent():
    s, m = session()
    real = m._mailbox

    def wrong(addr, data):
        real(addr, data)
        m.rx[-1] ^= 1 << 260  # corrupt the transaction tag

    m._mailbox = wrong
    with pytest.raises(fpga.HardwareError, match="got tag"):
        s.ctrl_read((1, 1), dev.CU_CAPS, timeout=2.0)


# ---- counters -----------------------------------------------------------
def test_the_base_counters_split_into_retirements_and_busy_cycles():
    assert dev.decode_counters((7 << 32) | 1234) == {"retired": 7, "busy_cycles": 1234}


def test_index_three_means_different_things_per_cu_type():
    """The pair is the DATAPATH's, so only its own type can decode it."""
    cl = dev.decode_dbg((600 << 32) | 500, dev.CU_MATMUL)
    assert cl == {
        "compute_cycles": 600,
        "memory_cycles": 500,
        "scope": "cumulative",
    }
    # vec_cu ties the high word to zero and its counter is cleared by every RUN.
    ve = dev.decode_dbg(370, dev.CU_VECTOR)
    assert ve["compute_cycles"] == 370 and ve["memory_cycles"] is None
    assert ve["scope"] == "last-run"
    assert dev.decode_dbg(0, 0x1234)["scope"] is None


def test_a_node_reports_compute_memory_and_dispatch_separately():
    """The measurement wall clock cannot make: one JTAG access dwarfs the run."""
    s, m = session()
    cl, ve = tuple(s.board.clusters[0]), tuple(s.board.vectors[0])
    m.ctr[cl] = (12, 1000)
    m.dbg[cl] = (600 << 32) | 500
    m.ctr[ve] = (3, 800)
    m.dbg[ve] = 370

    got = s.node_counters()
    row = got[f"{cl[0]},{cl[1]}"]
    assert (row["kind"], row["retired"], row["busy_cycles"]) == ("MG", 12, 1000)
    assert row["compute_cycles"] == 600 and row["memory_cycles"] == 500
    # Against the LARGER of the two, because a fill overlaps the array running.
    assert row["dispatch_cycles"] == 400
    assert got[f"{ve[0]},{ve[1]}"]["dispatch_cycles"] == 800 - 370


def test_a_node_that_does_not_answer_does_not_lose_the_others():
    s, m = session()
    m.nodes_present.pop(tuple(s.board.clusters[0]))
    got = s.node_counters()
    assert "error" in got["{},{}".format(*s.board.clusters[0])]
    assert "busy_cycles" in got["{},{}".format(*s.board.clusters[1])]


def test_cumulative_counters_are_differenced_and_last_run_ones_are_not():
    """`vec_cu` clears its kernel counter every RUN, so subtracting the previous
    read there would remove a run that is not in this measurement."""
    before = {
        "1,1": {
            "scope": "cumulative",
            "retired": 5,
            "busy_cycles": 100,
            "compute_cycles": 60,
            "memory_cycles": 30,
        },
        "3,3": {
            "scope": "last-run",
            "retired": 2,
            "busy_cycles": 400,
            "compute_cycles": 111,
            "memory_cycles": None,
        },
    }
    after = {
        "1,1": {
            "scope": "cumulative",
            "retired": 9,
            "busy_cycles": 350,
            "compute_cycles": 200,
            "memory_cycles": 130,
        },
        "3,3": {
            "scope": "last-run",
            "retired": 3,
            "busy_cycles": 900,
            "compute_cycles": 370,
            "memory_cycles": None,
        },
    }
    got = fpga.delta_counters(before, after)
    assert got["1,1"]["delta"] is True
    assert got["1,1"]["retired"] == 4 and got["1,1"]["busy_cycles"] == 250
    assert got["1,1"]["compute_cycles"] == 140 and got["1,1"]["memory_cycles"] == 100
    assert got["1,1"]["dispatch_cycles"] == 110
    # The last-run counter passes through; only the cumulative pair is differenced.
    assert got["3,3"]["compute_cycles"] == 370
    assert got["3,3"]["busy_cycles"] == 500


def test_the_cu_error_level_does_not_collide_with_the_unreachable_sentinel():
    """A bare `error` key means "this node did not answer". CU_STATUS's own
    error bit sharing that name made every reachable node look like a failed
    read, and `delta_counters` silently stopped differencing any of them."""
    assert "error" not in dev.decode_status(1 << 62)
    assert dev.decode_status(1 << 62)["cu_error"] == 1
    s, m = session()
    cl = tuple(s.board.clusters[0])
    m.ctr[cl] = (0, 100)
    before = s.node_counters()
    m.ctr[cl] = (0, 700)
    got = fpga.delta_counters(before, s.node_counters())
    assert got[f"{cl[0]},{cl[1]}"]["busy_cycles"] == 600


def test_a_node_missing_from_the_before_read_is_not_passed_off_as_a_delta():
    after = {
        "1,1": {
            "scope": "cumulative",
            "retired": 9,
            "busy_cycles": 350,
            "compute_cycles": 200,
            "memory_cycles": 130,
        }
    }
    got = fpga.delta_counters({}, after)
    assert got["1,1"]["delta"] is False and got["1,1"]["busy_cycles"] == 350


def test_a_counter_that_wrapped_does_not_report_negative_four_billion():
    """32 bits at a few hundred MHz wraps every ten seconds or so."""
    before = {
        "1,1": {
            "scope": "cumulative",
            "retired": 0,
            "busy_cycles": dev.MASK32 - 10,
            "compute_cycles": 0,
            "memory_cycles": 0,
        }
    }
    after = {
        "1,1": {
            "scope": "cumulative",
            "retired": 0,
            "busy_cycles": 5,
            "compute_cycles": 0,
            "memory_cycles": 0,
        }
    }
    assert fpga.delta_counters(before, after)["1,1"]["busy_cycles"] == 16


def test_counters_are_cheap_unless_asked_to_be_deep():
    """A CU_CTRL round trip per register per node, on a ~32 ms transport."""
    s, _ = session()
    assert "busy_cycles" not in next(iter(s.counters()["nodes"].values()))
    assert "busy_cycles" in next(iter(s.counters(deep=True)["nodes"].values()))


# ---- CU_DATA ------------------------------------------------------------
def test_a_burst_is_a_descriptor_then_one_flit_per_word():
    f = dev.cu_data_flits([0xAA, 0xBB, 0xCC], buf_id=2, offset=64, signal=True, txn=9)
    assert len(f) == 4
    for one in f:
        assert dev._field(one, 271, 4) == dev.T_CU_DATA
        assert dev._field(one, 267, 8) == 9
        assert one < (1 << dev.FLIT_BITS)
    desc = f[0]
    assert dev._field(desc, 255, 8) == 2  # buf_id
    assert dev._field(desc, 247, 16) == 64  # offset, in 32-byte granules
    assert dev._field(desc, 231, 8) == 2  # len = flits - 1
    assert dev._field(desc, 223, 8) & 1 == 1  # signal_on_complete
    # `last` marks the final DATA flit, never the descriptor.
    assert [dev._field(one, 259, 1) for one in f] == [0, 0, 0, 1]
    assert [one & ((1 << 256) - 1) for one in f[1:]] == [0xAA, 0xBB, 0xCC]


def test_the_completion_can_be_pointed_somewhere_other_than_the_sender():
    """A CU answering its SENDER is useless when the sender is another CU --
    nothing there consumes it, so the host cannot sequence a reader behind a
    writer. The payload stays on the mesh; only the ack is redirected."""
    plain = dev.cu_data_flits([0])[0]
    assert dev._field(plain, 215, 8) == 0, "0 means the descriptor's source"
    aimed = dev.cu_data_flits([0], ack=(1, 2))[0]
    assert dev._field(aimed, 215, 8) == (2 << 4) | 1  # {ack_y, ack_x}


def test_the_ack_sentinel_cannot_be_confused_with_a_real_coordinate():
    """(0,0) is a mesh corner: it touches no router, so nothing can live there."""
    with pytest.raises(ValueError, match="sentinel"):
        dev.cu_data_flits([0], ack=(0, 0))
    with pytest.raises(ValueError, match="ack x"):
        dev.cu_data_flits([0], ack=(16, 1))


def test_the_dispatcher_stamps_the_route_so_the_encoder_leaves_it_clear():
    """noc_orchestrator overwrites dst from PROG_DST and src with its own
    coordinates, and touches nothing else. Staging a route here would be
    overwritten, and staging the WRONG type would not be."""
    desc = dev.cu_data_flits([0])[0]
    assert dev._field(desc, 287, 4) == 0 and dev._field(desc, 283, 4) == 0
    assert dev._field(desc, 279, 4) == 0 and dev._field(desc, 275, 4) == 0


@pytest.mark.parametrize(
    "kw,match",
    [
        ({"words": []}, "at least one"),
        ({"words": [0] * 257}, "at most 256"),
        ({"words": [0], "buf_id": 256}, "buf_id"),
        ({"words": [0], "offset": 1 << 16}, "offset"),
    ],
)
def test_a_burst_that_cannot_be_encoded_is_refused(kw, match):
    with pytest.raises(ValueError, match=match):
        dev.cu_data_flits(**kw)


def test_pushing_l1_waits_for_the_receivers_own_data_received():
    """The host is the descriptor's source, so the ack comes back to us."""
    s, m = session()
    node = tuple(s.board.vectors[0])
    st = s.push_l1(node, [0x1111, 0x2222], buf_id=0, offset=64)
    assert st["code"] == dev.SIG_DATA_RECEIVED and st["arg"] == 0
    # ONE ack for the whole burst, not one per flit.
    assert st["signals"] == 1
    assert m.kicks == [(dev.node_index(*node), 3)]


def test_a_data_burst_seeds_credit_for_its_own_length():
    """Credit is spent per flit and refilled only by SIG_INST_COMPLETE, which a
    data flit never sends. Seeded at INST_DEPTH a longer burst stalls forever."""
    s, m = session()
    s.push_l1(tuple(s.board.vectors[0]), [0] * 40)
    assert m.mem[s.board.ctrl(dev.A_PROG_CRED)] == 41
    assert 41 > dev.INST_DEPTH, "the point is that it exceeds the instruction bound"


def test_a_burst_too_long_for_the_staging_ram_is_refused_before_the_card():
    s, m = session()
    with pytest.raises(fpga.HardwareError, match="staging RAM"):
        s.push_l1(tuple(s.board.vectors[0]), [0] * (s.board.stage_flits + 1))
    assert not m.kicks


def test_a_round_can_wait_per_cluster_instead_of_on_the_shared_counter():
    """`A_SIG_DONE` counts EVERY node, so a vector core retiring a kernel or
    answering a CU_DATA burst can release a cluster round early -- and the next
    upload then overwrites staging under a dispatcher still streaming."""
    from ktpu.hw import kernel

    passes = bench.build(16, 32, 16, ncl=2, preq=(False, False)).kern.rounds[0].passes
    shared = kernel.control(passes)
    assert any(c.addr == dev.MAG_BASE + dev.A_SIG_DONE for c in shared.cmds)

    base = {tuple(p.cluster): 500 for p in passes}
    per = kernel.control(passes, baseline=base)
    polls = [c for c in per.cmds if c.op == dev.OP_POLL and c.mask == 0x00FF_FF01]
    assert polls, "a per-cluster wait must poll NODE_STATUS"
    for c in polls:
        assert c.addr != dev.MAG_BASE + dev.A_SIG_DONE
        # The target is baseline + this round's flits, because the count is
        # cumulative and nothing clears it.
        assert (c.data >> 8) & 0xFFFF > 500
    # Nothing is cleared either: clearing SIG_DONE would not clear these.
    assert not any(
        c.addr == dev.MAG_BASE + dev.A_SIG_DONE and c.op == dev.OP_WR for c in per.cmds
    )


def test_await_node_targets_are_absolute_because_nothing_clears_them():
    """`node_status` is written on every CU_SIGNAL and cleared by nothing --
    not by SIG_DONE, not by resetn. A second round asking for the same count
    waits for a number the register has already passed."""
    (cmd,) = dev.Program().await_node_at(2, 3, 0x1_0005).cmds
    assert cmd.addr == dev.node_status_addr(2, 3)
    assert cmd.data == (5 << 8) | 1, "16-bit counter, so the target wraps too"


def test_await_signal_matches_the_code_and_not_only_the_count():
    """A count alone is satisfied by any signal that node happens to emit,
    a fault included."""
    p = dev.Program().await_signal(1, 2, dev.SIG_DATA_RECEIVED, 7, arg=3)
    (cmd,) = p.cmds
    assert cmd.addr == dev.node_status_addr(1, 2)
    assert cmd.data == (0x03 << 56) | (3 << 24) | (7 << 8) | 1
    assert cmd.mask == 0xFFFF_FFFF_FFFF_FF01
    # Without an arg the argument field is not constrained.
    (loose,) = dev.Program().await_signal(1, 2, dev.SIG_DATA_RECEIVED, 7).cmds
    assert loose.mask == 0xFF00_0000_00FF_FF01


# ---- check --------------------------------------------------------------
def test_a_shape_below_one_subtile_is_refused():
    s, _ = session()
    assert any("minimum shape" in w for w in s.check(2, 32, 16))


def test_prequantisation_is_refused_on_a_board_that_cannot_mark():
    """The silent-corruption case: FP16 into a region sized for MXFP7."""
    s, _ = session()
    why = s.check(16, 32, 16, preq=(True, True))
    assert any("sized for MXFP7" in w for w in why), why


def test_the_board_capacity_replaces_the_bench_file_limits():
    """4 GB of DDR, not an 8 MB axi_ram -- a shape the bench cannot hold runs here."""
    s, _ = session()
    big = 2048, 1024, 1024
    assert s.check(*big) == []
    # Same preq on both sides, or the comparison is between two footprints.
    why = bench.check(*big, ncl=2, preq=fpga.PREQ)
    assert any("bench RAM" in w for w in why), why


def test_dispatching_more_clusters_than_the_board_has_is_refused():
    s, _ = session()
    assert any("cannot dispatch to 5" in w for w in s.check(16, 32, 16, use=5))


# ---- a whole run --------------------------------------------------------
def test_a_run_goes_end_to_end_and_scores_like_a_simulation():
    s, _ = session()
    r = s.run(16, 32, 16)

    assert r["shape"] == {"m": 16, "k": 32, "n": 16}
    assert r["ncl"] == 2 and r["ok"] is True
    assert r["verdict"]["pass"] in (True, False), "the simulator's verdict shape"
    assert r["board"]["name"] == BOARD
    assert r["counters"]["sig_done"] == sum(len(p.flits) for p in _flits(16, 32, 16))
    assert np.asarray(r["got"]["data"]).shape[0] > 0
    # No bench counters exist on hardware, and none are invented.
    assert r["profile"] is None
    assert r["telemetry"].get("run_cycles") is None
    assert r["node_counters"] is None, "the deep read is opt-in"


def test_a_profiled_run_attaches_the_per_node_cycle_split():
    """What makes GFLOP/s measurable: cycles from the nodes, not from a clock."""
    s, m = session()
    cl = tuple(s.board.clusters[0])
    m.ctr[cl] = (0, 100)
    m.dbg[cl] = (10 << 32) | 5

    def advance(*_):
        m.ctr[cl] = (4, 900)
        m.dbg[cl] = (610 << 32) | 205

    real = s._round
    s._round = lambda *a: (advance(), real(*a))[1]
    r = s.run(16, 32, 16, profile=True)

    row = r["node_counters"][f"{cl[0]},{cl[1]}"]
    assert row["compute_cycles"] == 600 and row["memory_cycles"] == 200
    assert row["busy_cycles"] == 800 and row["dispatch_cycles"] == 200


def test_a_run_writes_only_inside_the_boards_windows():
    """Every byte goes to the ctrl window or to DDR through MAG, and nowhere else."""
    s, m = session()
    s.run(16, 32, 16)
    ctrl = range(0x4_0080_0000, 0x4_0081_0000)
    for addr in m.mem:
        assert addr in ctrl or addr < s.board.mem_words * 32, hex(addr)


def test_a_run_kicks_every_cluster_the_board_has():
    s, m = session()
    s.run(16, 32, 16)
    assert m.mem[s.board.ctrl(dev.A_PROG_KICK)] == 1
    assert m.mem[s.board.ctrl(dev.A_PROG_CRED)] == dev.INST_DEPTH


def test_the_operands_go_through_mags_memory_window_unmarked():
    """preq is off by default here, so no address may carry a marker bit."""
    s, m = session()
    s.run(16, 32, 16)
    written = [a for a in m.mem if a < 0x4_0000_0000]
    assert written, "no operands reached memory"
    assert all(a < (1 << 32) for a in written), "a marker bit was set"


def test_the_upload_is_blocks_and_not_a_word_at_a_time():
    """One DMA per operand region. At a word each this would be unusable."""
    s, m = session()
    prob = bench.build(16, 32, 16, ncl=2, preq=(False, False))
    s.upload(prob)
    _, regions = bench.upload(prob)
    assert len(m.blocks) == len(regions)


def test_prefill_marks_the_whole_image():
    """ECC makes a never-written line a bus error, so the image is claimed first."""
    s, m = session()
    prob = bench.build(16, 32, 16, ncl=2, preq=fpga.PREQ)
    _, end = bench.plan(prob.m, prob.k, prob.n, prob.tile, prob.kern.preq)
    assert s.prefill(prob) == end * 32
    assert m.blocks == [(0, end * 32)]
    assert m.mem[0] == 0xDEAD_BEEF_DEAD_BEEF


def test_an_ecc_error_on_readback_is_explained_rather_than_raw():
    """What a machine that drained nothing looks like, on a DRAM with ECC."""
    s, m = session()
    prob = bench.build(16, 32, 16, ncl=2, preq=fpga.PREQ)

    def boom(addr, nbytes):
        raise OSError("RRESP=SLVERR")

    m.read_block = boom
    with pytest.raises(fpga.HardwareError, match="uncorrectable ECC"):
        s.read_c(prob)


def test_results_are_read_back_from_the_c_region_only():
    """A and B are already known; reading them back would double the run."""
    s, m = session()
    prob = bench.build(16, 32, 16, ncl=2, preq=(False, False))
    layout, end = bench.plan(prob.m, prob.k, prob.n, prob.tile, prob.kern.preq)
    words = s.read_c(prob)
    assert len(words) == end
    assert m.blocks == [(s.board.mem_byte(layout.c_word), (end - layout.c_word) * 32)]


# ---- how a round is awaited ---------------------------------------------
def test_a_run_waits_per_cluster_and_not_on_the_shared_counter():
    s, m = session()
    s.run(16, 32, 16)
    polled = {a for a, _ in m.reads.items()}
    nodes = {
        s.board.ctrl(dev.A_NODE + dev.node_index(x, y) * 8) for x, y in s.board.clusters
    }
    assert polled & nodes, "no cluster's own NODE_STATUS was ever read"


def test_the_old_shared_wait_is_still_reachable_for_an_a_b_on_the_card():
    """Ruling the intermittent in or out means running it both ways."""
    s, m = session()
    s.run(16, 32, 16, strict_wait=False)
    assert m.mem[s.board.ctrl(dev.A_SIG_DONE)] == 0, "the shared path clears it"


def test_a_stall_is_still_reported_on_both_wait_paths():
    """A hang must not become invisible on the path that is now the default."""
    for strict in (True, False):
        s, _ = session(stall=True)
        with pytest.raises(fpga.HardwareError, match="did not retire"):
            s.run(16, 32, 16, strict_wait=strict)


def test_the_round_cutter_still_measures_the_shared_form():
    """Round BOUNDARIES must not move: `cut_rounds` sizes a round against the
    command RAM, and only the host-executed program gets the per-cluster form
    -- where there is no command RAM, because no synthesised mesh has one."""
    a = bench.build(256, 128, 256, ncl=4, preq=(False, False)).kern
    b = bench.build(256, 128, 256, ncl=4, preq=(False, False)).kern
    assert [len(r.passes) for r in a.rounds] == [len(r.passes) for r in b.rounds]
    # The delta is (distinct clusters - 2) commands, and it lands nowhere that
    # is bounded by `ncmd`.
    rnd = a.rounds[0]
    cl = {tuple(p.cluster) for p in rnd.passes}
    shared = len(kernel.control(rnd.passes))
    strict = len(kernel.control(rnd.passes, baseline={c: 0 for c in cl}))
    assert strict - shared == len(cl) - 2


def test_a_foreign_signal_cannot_release_a_strict_round():
    """THE INTERMITTENT. `A_SIG_DONE` counts every node, so a vector core
    retiring a kernel -- or answering a CU_DATA burst -- satisfies a wait sized
    for the clusters, and the next upload overwrites staging under a dispatcher
    still streaming. Per cluster, somebody else's traffic is invisible."""
    _, m = session()
    passes = bench.build(16, 32, 16, ncl=2, preq=(False, False)).kern.rounds[0].passes
    base = {
        tuple(p.cluster): m.nodes.get(dev.node_index(*p.cluster), 0) for p in passes
    }

    shared = kernel.control(passes)
    strict = kernel.control(passes, baseline=base)
    sig_done = dev.MAG_BASE + dev.A_SIG_DONE
    assert any(c.addr == sig_done and c.op == dev.OP_POLL for c in shared.cmds)
    assert not any(c.addr == sig_done for c in strict.cmds)

    # One poll per DISTINCT cluster, however many passes each one carries.
    polls = [c for c in strict.cmds if c.op == dev.OP_POLL and c.mask == 0x00FF_FF01]
    assert len(polls) == len({tuple(p.cluster) for p in passes})


def test_the_strict_wait_targets_are_baseline_plus_this_rounds_flits():
    """`node_status` is cumulative and nothing clears it, so a target that
    ignored the baseline would wait for a count already gone past."""
    passes = bench.build(16, 32, 16, ncl=2, preq=(False, False)).kern.rounds[0].passes
    want = {}
    for p in passes:
        c = tuple(p.cluster)
        want[c] = want.get(c, 0) + len(p.flits)
    strict = kernel.control(passes, baseline={c: 900 for c in want})
    got = {
        c.addr: (c.data >> 8) & 0xFFFF
        for c in strict.cmds
        if c.op == dev.OP_POLL and c.mask == 0x00FF_FF01
    }
    for c, n in want.items():
        assert got[dev.node_status_addr(*c)] == 900 + n


def test_the_staged_flits_are_identical_whichever_wait_is_used():
    """Only the control commands differ; what the machine computes cannot."""
    a, ma = session()
    b, mb = session()
    a.run(16, 32, 16, strict_wait=True)
    b.run(16, 32, 16, strict_wait=False)
    stage = range(a.board.ctrl(dev.A_STAGE), a.board.ctrl(dev.A_STAGE) + 0x2000)
    sa = {k: v for k, v in ma.mem.items() if k in stage}
    sb = {k: v for k, v in mb.mem.items() if k in stage}
    assert sa == sb and sa, "the staging RAM must not depend on the wait"


# ---- failures -----------------------------------------------------------
def test_a_round_that_never_completes_reports_the_machines_own_state():
    """A hang must name the register and the per-cluster counters, not just time out."""
    s, _ = session(stall=True)
    with pytest.raises(fpga.HardwareError) as exc:
        s.run(16, 32, 16)
    text = str(exc.value)
    assert "did not retire" in text and "counters:" in text
    assert "sig_done" in text and "nodes" in text


def test_an_unknown_distribution_is_refused_before_the_card_is_touched():
    s, m = session()
    with pytest.raises(ValueError, match="unknown distribution"):
        s.run(16, 32, 16, dist="gaussian")
    assert not m.mem, "nothing may reach the card before the arguments are checked"


# ---- wiring -------------------------------------------------------------
def test_a_session_needs_a_board_and_says_so(monkeypatch):
    monkeypatch.delenv(bd.BOARD_ENV, raising=False)
    with pytest.raises(ValueError, match="no board"):
        fpga.Session()


def test_the_board_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(bd.BOARD_ENV, BOARD)
    monkeypatch.setenv("KTPU_TRANSPORT", "memory")
    assert fpga.Session().board.name == BOARD


def _bench_globals():
    return {
        k: getattr(bench, k)
        for k in ("LAYOUT", "TILES", "L1_A_ENTRIES", "L1_B_ENTRIES", "STAGE_FLITS")
    }


def test_every_patched_global_is_restored_even_when_a_run_fails():
    """These are process-wide; leaking one would mis-plan the SIMULATOR."""
    was = _bench_globals()
    s, _ = session(stall=True)
    with pytest.raises(fpga.HardwareError):
        s.run(16, 32, 16)
    assert _bench_globals() == was


def test_a_run_plans_for_the_boards_capacities_and_not_the_rtls():
    """The bug that put 15,440 of 16,384 elements past 10% error on silicon.

    The shipped board is TILES=256 and the RTL is 512, so the two disagree
    about the tile for the same shape. What the machine is given must be the
    board's answer -- planning against the RTL's overruns the CU's tile memory
    silently and the run still reports success.
    """
    board = bd.Board.named(BOARD)
    assert board.tiles != bench.TILES, "this test needs the two to disagree"
    theirs = bench.tile_for(128, 64, 128, board.caps())
    rtls = bench.tile_for(128, 64, 128)
    assert theirs != rtls
    assert theirs.gm * theirs.gn <= board.tiles
    assert rtls.gm * rtls.gn > board.tiles, "the RTL's tile really does over-run"


def test_the_planner_takes_capacities_rather_than_reading_a_global():
    """The argument decides, not module state -- that is the point of threading it.

    Patching the globals to the board's numbers must change NOTHING about a
    call that passes the RTL's, and vice versa. While capacities were globals
    the two were indistinguishable and a session had to hold six of them.
    """
    board = bd.Board.named(BOARD)
    rtl = bench.current_caps()
    with fpga._globals(TILES=board.tiles, L1_A_ENTRIES=board.l1_a):
        assert bench.tile_for(128, 64, 128, rtl) == bench.tile_for(128, 64, 128, rtl)
        assert bench.tile_for(128, 64, 128, board.caps()) != bench.tile_for(
            128, 64, 128, rtl
        )


def test_check_measures_the_shape_against_the_boards_capacities():
    """A shape whose tile the board cannot hold must not be planned for it."""
    s, _ = session()
    tile = bench.tile_for(128, 64, 128, s.board.caps())
    assert tile.gm * tile.gn <= s.board.tiles
    assert s.check(128, 64, 128) == []


def test_the_session_builds_for_the_boards_layout_not_the_globals():
    """The board is split and the default is merged; the program must follow the board."""
    assert bench.LAYOUT == "merged"
    s, m = session()
    s.run(16, 32, 16)
    want = {dev.node_index(x, y) for x, y in s.board.clusters}
    assert m.mem[s.board.ctrl(dev.A_PROG_DST)] in want
