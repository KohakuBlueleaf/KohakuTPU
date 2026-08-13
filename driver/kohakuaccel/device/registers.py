"""The address map and wire-level constants every KohakuAccel machine has.

Nothing here describes a compute unit's datapath. These are the registers the
memory agent and the control agent expose, the flit type codes, and the
centrally allocated signal codes -- the FIXED PROTOCOL half of the framework, in
the sense ``docs/integrate/what-you-own.md`` uses the phrase.

The RTL side of these definitions is ``src/kohakunoc/noc_pkt.vh``. That header is
included by nothing and every module restates it by hand; this file is a further
restatement, correct by agreement rather than by construction. See
the framework/project split, F1.
"""

# Bit 28 selects the memory agent over the main orchestrator.
ORC_BASE = 0x0000_0000
MAG_BASE = 0x1000_0000

# The main orchestrator: the control-program engine.
MO_CTRL = 0x0000  # W: [0] GO      R: [0] busy [1] done [2] err
MO_PC = 0x0008  # R: current command index
MO_CODE = 0x0010  # R: the DONE code
MO_POLLS = 0x0018  # R: polls executed
MO_CMD = 0x1000  # command n, field f at MO_CMD + n*32 + f*8

# The control agent inside the memory agent.
A_CTRL = 0x0000
A_STATUS = 0x0008  # R: [0] busy [1] error [2] mesh_ready
# {GRID_HI, GRID_LO, POS_WIDTH, FLIT_WIDTH}; the only register whose right
# answer is known before the machine has run.
A_CAPS = 0x0010
A_PROG_DST = 0x0040
A_PROG_LEN = 0x0048
A_PROG_KICK = 0x0050
A_PROG_STAT = 0x0058  # R: [0] run, [16:1] flits left, [32:17] credit
A_PROG_CRED = 0x0060
A_PROG_BASE = 0x0068  # first staging slot of this program
A_SIG_DONE = 0x0070  # R: completions from every node; W: clear
A_NODE = 0x1000  # + {y,x}*8
A_STAGE = 0x2000  # instruction flits, 5 x 64-bit words each

# The raw-flit mailbox: the only path to a node that is not a dispatch, and so
# the only way to ask a unit what it is rather than assuming.
A_TX_FLIT0 = 0x0100  # 5 x 64-bit, low word first
A_TX_KICK = 0x0140
A_TX_STATUS = 0x0148  # R: [16] tx_full
A_RX_FLIT0 = 0x0180
A_RX_POP = 0x01C0
A_RX_STATUS = 0x01C8  # R: [16] empty [17] overflow

T_MEM_RD_REQ = 0x0
T_MEM_WR_REQ = 0x1
T_MEM_RD_RESP = 0x2
T_MEM_WR_ACK = 0x3
T_MEM_WR_DATA = 0x4
T_CU_INST = 0x5
T_CU_SIGNAL = 0x6
T_CU_CTRL = 0x7
# 0x8, not 0x4: 0x4 is MEM_WR_DATA, and a CU_DATA flit carrying it entered the
# agent's write queue as data. noc_cu_null.v:49 still has this wrong.
T_CU_DATA = 0x8
T_ERROR = 0xF

# The control registers every endpoint answers whatever it computes. This is the
# whole of what the framework requires a unit to expose.
CU_CAPS = 0  # {CU_TYPE[16], CU_VERSION[8], N_BUFFERS[4], INST_DEPTH[16], 20'd0}
CU_STATUS = 1
CU_COUNTERS = 2  # {instructions_retired[32], busy_cycles[32]}, kept in the base
CU_DBG = 3  # the datapath's own pair; what it means is per unit type
CTRL_READ_RESP = 2

# Below 0x40 is centrally allocated; above it is unit-defined.
SIG_INST_COMPLETE = 0x00
SIG_BATCH_COMPLETE = 0x01
SIG_BARRIER_REACHED = 0x02
SIG_DATA_RECEIVED = 0x03
SIG_FAULT = 0x04

# A burst is a descriptor flit then `len+1` data flits, and `len` is 8 bits
# holding count-minus-one.
CUD_MAX_WORDS = 256

FLIT_BITS = 288
POS_BITS = 4
FLIT_WORDS = 5  # 64-bit words per staged instruction flit

OP_WR = 1
OP_POLL = 2
OP_DONE = 3

# noc_cu_base's instruction FIFO depth, and 512 is a FLOOR rather than a choice:
# a BRAM cannot be shallower. Read the real value per unit from CU_CAPS.
INST_DEPTH = 512


def node_index(x: int, y: int) -> int:
    """Mesh coordinate -> the {y,x} byte used by PROG_DST and NODE_STATUS."""
    return ((y & 0xF) << 4) | (x & 0xF)


def node_status_addr(x: int, y: int) -> int:
    """Byte address of one node's NODE_STATUS mirror."""
    return MAG_BASE + A_NODE + node_index(x, y) * 8


def type_code(name: str) -> int:
    """Two ASCII characters -> the 16-bit CU_TYPE they encode.

    Unit types are ASCII so an unknown endpoint reports something readable
    rather than a bare number. Raises :class:`ValueError` unless `name` is
    exactly two ASCII characters.
    """
    raw = name.encode("ascii")
    if len(raw) != 2:
        raise ValueError(f"a unit type is exactly two ASCII characters, got {name!r}")
    return (raw[0] << 8) | raw[1]
