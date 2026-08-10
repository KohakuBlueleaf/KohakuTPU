#!/usr/bin/env python3
"""Generate a mesh top module from a picture of the mesh.

    python scripts/py/gen_mesh.py maps/mesh_2x2.txt -o src/synth_top/ktpu_mesh.v

The map is a grid of THREE-CHARACTER tokens separated by spaces or dashes:

    xxx vec vec xxx
    mag mat mat vec
    mag mat mat vec
    xxx vec vec xxx

The interior is the router grid; the first and last row are the north and south
edges, the first and last column the west and east edges. Routers sit at
(1..NX, 1..NY) and edge endpoints just outside, at (x,0), (x,NY+1), (0,y) and
(NX+1,y) -- the router's coordinate clamp is what makes those reachable.

  xxx  nothing here. REQUIRED at the four corners: a corner touches no router,
       so a token there would have nowhere to attach.
  nul  the port exists but is tied off. This is how you leave a side empty
       without changing the grid's shape.
  mag  one of MAG's NoC memory ports. At most four -- mag.v declares exactly
       four port coordinate pairs.
  vec  a vector core.
  mat  a matmul cluster, one mx_cluster_cu on one router local.

`mgr` and `acu` are RETIRED, not deprecated. They named the two locals of a
two-port cluster, and mx_cluster_cu presents one; a pair form would have no
module to emit. A map using them is rejected by name.

Non-square grids are supported: the emitted module sets GRID_X_HI and
GRID_Y_HI separately, which is why noc_inport.v clamps each axis on its own.
"""

import argparse
import sys
from pathlib import Path

EMPTY = {"xxx", "   ", "..."}
TIED = EMPTY | {"nul"}
KNOWN = TIED | {"mag", "vec", "mat"}
# Named so the error says what to write instead. Falling through to "unknown
# token" would read as a typo rather than as a shape the hardware dropped.
RETIRED = {"mgr", "acu"}


class MeshError(Exception):
    pass


def parse_map(text):
    """Parse the token picture into a list of rows of 3-char tokens.

    Raises MeshError on ragged rows, bad token widths or unknown tokens.
    """
    rows = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        toks = [t for t in line.replace("-", " ").split(" ") if t]
        for t in toks:
            if len(t) != 3:
                raise MeshError(f"line {lineno}: token {t!r} is {len(t)} chars, want 3")
            if t in RETIRED:
                raise MeshError(
                    f"line {lineno}: {t!r} named one local of the two-port cluster, "
                    "which no longer exists; a cluster is one 'mat'"
                )
            if t not in KNOWN:
                raise MeshError(
                    f"line {lineno}: unknown token {t!r}; "
                    f"known are {' '.join(sorted(KNOWN - {'   '}))}"
                )
        rows.append(toks)

    if len(rows) < 3:
        raise MeshError("need at least 3 rows: north edge, one router row, south edge")
    width = len(rows[0])
    for i, r in enumerate(rows):
        if len(r) != width:
            raise MeshError(f"row {i} has {len(r)} tokens, row 0 has {width}")
    if width < 3:
        raise MeshError(
            "need at least 3 columns: west edge, one router column, east edge"
        )
    return rows


class Mesh:
    """The parsed map, resolved into routers, endpoints and their coordinates."""

    def __init__(self, rows):
        self.rows = rows
        self.ny = len(rows) - 2
        self.nx = len(rows[0]) - 2
        self._check_corners()
        self.mags = []  # [(x, y)] in map coordinates
        self.vecs = []  # [(x, y)]
        self.cus = []  # [(x, y)] -- one router local each
        self._classify()

    def tok(self, x, y):
        """Token at map coordinate (x, y); (0,0) is the north-west corner."""
        return self.rows[y][x]

    def _check_corners(self):
        for x, y in (
            (0, 0),
            (self.nx + 1, 0),
            (0, self.ny + 1),
            (self.nx + 1, self.ny + 1),
        ):
            if self.tok(x, y) not in EMPTY:
                raise MeshError(
                    f"corner ({x},{y}) is {self.tok(x, y)!r}; corners touch no router "
                    "and must be xxx"
                )

    def _classify(self):
        for y in range(self.ny + 2):
            for x in range(self.nx + 2):
                t = self.tok(x, y)
                if t in TIED:
                    continue
                if t == "mag":
                    self.mags.append((x, y))
                elif t == "vec":
                    self.vecs.append((x, y))
                elif t == "mat":
                    if not self._interior(x, y):
                        raise MeshError(
                            f"mat at ({x},{y}) is on an edge; a cluster "
                            "needs a router local"
                        )
                    self.cus.append((x, y))

        if len(self.mags) > 4:
            raise MeshError(f"{len(self.mags)} mag tiles; mag.v declares only 4 ports")
        if not self.mags:
            raise MeshError("no mag tile: nothing would serve memory")

    def _interior(self, x, y):
        return 1 <= x <= self.nx and 1 <= y <= self.ny

    def local(self, x, y):
        """What occupies router (x,y)'s local port, or None."""
        t = self.tok(x, y)
        return t if t not in TIED else None

    def nearest_mag(self, x, y):
        """The mag port a node at (x,y) should address, by clamped Manhattan hops.

        Distance is measured on the clamped coordinates because that is what the
        router actually routes on -- an edge node and the router it hangs off
        are the same point as far as hop count goes.
        """

        def clamp(p, hi):
            return min(max(p, 1), hi)

        cx, cy = clamp(x, self.nx), clamp(y, self.ny)
        best = min(
            self.mags,
            key=lambda m: abs(clamp(m[0], self.nx) - cx)
            + abs(clamp(m[1], self.ny) - cy),
        )
        return best


class Emitter:
    """Builds the Verilog text: link wires, then instances."""

    def __init__(self, mesh, name):
        self.m = mesh
        self.name = name
        self.links = {}  # key -> link id
        self.decls = []
        self.body = []

    def link(self, key):
        """The wire-name stem for a link, creating it on first use.

        A link joins exactly two port-ends. `fwd` runs from the end that named
        it first; `rev` comes back. Busy always travels against its data.
        """
        if key not in self.links:
            n = f"l{len(self.links):03d}_{key}"
            self.links[key] = n
            self.decls.append(
                f"    wire [FW-1:0] {n}_fd; wire {n}_fv; wire {n}_fb;\n"
                f"    wire [FW-1:0] {n}_rd; wire {n}_rv; wire {n}_rb;"
            )
        return self.links[key]

    def hlink(self, x, y):
        """Horizontal link west of router column x+1, i.e. between x and x+1."""
        return self.link(f"h{x}_{y}")

    def vlink(self, x, y):
        """Vertical link north of router row y+1."""
        return self.link(f"v{x}_{y}")

    # A router drives `fwd` on its east/south sides and `rev` on west/north, so
    # every link has exactly one driver per direction.
    def router_ports(self, x, y):
        w, e = self.hlink(x - 1, y), self.hlink(x, y)
        n, s = self.vlink(x, y - 1), self.vlink(x, y)
        loc = self.link(f"L{x}_{y}")
        return f"""        .west_in_data({w}_fd),   .west_in_valid({w}_fv),   .west_in_busy({w}_fb),
        .west_out_data({w}_rd),  .west_out_valid({w}_rv),  .west_out_busy({w}_rb),
        .east_in_data({e}_rd),   .east_in_valid({e}_rv),   .east_in_busy({e}_rb),
        .east_out_data({e}_fd),  .east_out_valid({e}_fv),  .east_out_busy({e}_fb),
        .north_in_data({n}_fd),  .north_in_valid({n}_fv),  .north_in_busy({n}_fb),
        .north_out_data({n}_rd), .north_out_valid({n}_rv), .north_out_busy({n}_rb),
        .south_in_data({s}_rd),  .south_in_valid({s}_rv),  .south_in_busy({s}_rb),
        .south_out_data({s}_fd), .south_out_valid({s}_fv), .south_out_busy({s}_fb),
        .local_in_data({loc}_fd),  .local_in_valid({loc}_fv),  .local_in_busy({loc}_fb),
        .local_out_data({loc}_rd), .local_out_valid({loc}_rv), .local_out_busy({loc}_rb)"""

    def edge_link(self, x, y):
        """(stem, endpoint_drives_fwd) for the link an edge tile at (x,y) uses."""
        m = self.m
        if y == 0:
            return self.vlink(x, 0), True  # north tile drives southward
        if y == m.ny + 1:
            return self.vlink(x, m.ny), False  # south tile drives northward
        if x == 0:
            return self.hlink(0, y), True  # west tile drives eastward
        return self.hlink(m.nx, y), False  # east tile drives westward

    def endpoint_conn(self, stem, drives_fwd, names):
        """Port connections for an endpoint, given which direction it drives.

        `names` is (in_data, in_valid, in_busy, out_data, out_valid, out_busy).
        """
        i_d, i_v, i_b, o_d, o_v, o_b = names
        a, b = ("f", "r") if drives_fwd else ("r", "f")
        return (
            f".{o_d}({stem}_{a}d), .{o_v}({stem}_{a}v), .{o_b}({stem}_{a}b), "
            f".{i_d}({stem}_{b}d), .{i_v}({stem}_{b}v), .{i_b}({stem}_{b}b)"
        )


def emit(mesh, name):
    m, e = mesh, Emitter(mesh, name)
    nm = len(m.mags) + 2

    # ---- routers -------------------------------------------------------
    for y in range(1, m.ny + 1):
        for x in range(1, m.nx + 1):
            e.body.append(
                # DEPTH 512, NOT 32. A 288-bit port is 4 RAMB36 at ANY depth to
                # 512, so 32 used 6.25% of the memory it already paid for.
                # Measured on one router: 20 BRAM either way, +130 LUT, -1.8 MHz.
                f"    NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(512),\n"
                f'                .MEMORY_TYPE("block"), .POS_WIDTH(PW),\n'
                f"                .POS_X({x}), .POS_Y({y}), .GRID_LO(1),\n"
                f"                .GRID_HI({max(m.nx, m.ny)}), .GRID_X_HI({m.nx}),\n"
                f"                .GRID_Y_HI({m.ny})) r{x}_{y} (\n"
                f"        .clk(clk), .rst(rst),\n"
                f"{e.router_ports(x, y)}\n    );"
            )

    # ---- MAG -----------------------------------------------------------
    # Its NoC ports are one packed bus, so they are gathered here in port order
    # and sliced by index. Verilog concatenation is most-significant first, so
    # the list is reversed.
    mag_in, mag_out = [], []
    for i, (x, y) in enumerate(m.mags):
        stem, fwd = e.edge_link(x, y)
        a, b = ("f", "r") if fwd else ("r", "f")
        mag_out.append(f"{stem}_{a}d")
        mag_in.append(f"{stem}_{b}d")
        e.body.append(f"    assign {stem}_{a}v = mag_o_v[{i}];")
        e.body.append(f"    assign mag_o_b[{i}] = {stem}_{a}b;")
        e.body.append(f"    assign mag_i_v[{i}] = {stem}_{b}v;")
        e.body.append(f"    assign {stem}_{b}b = mag_i_b[{i}];")
    for i, w in enumerate(mag_out):
        e.body.append(f"    assign {w} = mag_o_d[{i}*FW +: FW];")
    mag_in_cat = "{" + ", ".join(reversed(mag_in)) + "}"

    coords = "".join(
        f", .MEM_X{i or ''}({x}), .MEM_Y{i or ''}({y})"
        for i, (x, y) in enumerate(m.mags)
    )
    e.body.append(
        f"""    mag #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW),
          .ID_W(IDW), .MEM_PORTS({len(m.mags)}){coords},
          .GRID_LO(1), .GRID_HI({max(m.nx, m.ny)}), .STAGE_FLITS(128)) u_mag (
        .clk(clk), .resetn(resetn),
{MAG_AXI}
        .mem_in_data({mag_in_cat}), .mem_in_valid(mag_i_v), .mem_in_busy(mag_i_b),
        .mem_out_data(mag_o_d), .mem_out_valid(mag_o_v), .mem_out_busy(mag_o_b),
        .mem_rd_count(mag_rd), .mem_wr_count(mag_wr),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done)
    );"""
    )

    # ---- clusters ------------------------------------------------------
    NAMES_C = (
        "noc_in_data",
        "noc_in_valid",
        "noc_in_busy",
        "noc_out_data",
        "noc_out_valid",
        "noc_out_busy",
    )
    for i, (cx, cy) in enumerate(m.cus):
        # An endpoint on a router's LOCAL port drives `rev`: the router named
        # the link and took `fwd` for its own local_in.
        stem = e.link(f"L{cx}_{cy}")
        mmx, mmy = m.nearest_mag(cx, cy)
        # TILES/GA/GB PASSED EXPLICITLY. Omitted, mx_cluster_cu's defaults of
        # 256/32/32 elaborate instead of the 512/128/256 the compiler plans for
        # -- and that mismatch is the 15,440-element silicon fault, silently.
        e.body.append(f"""    mx_cluster_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                    .CU_X({cx}), .CU_Y({cy}), .TILES(TILES), .GA(GA), .GB(GB),
                    .L1_PRIM(L1_PRIM),
                    .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH),
                    .MEM_X({mmx}), .MEM_Y({mmy}), .MODEL(MODEL)) u_cu{i} (
        .clk(clk), .resetn(resetn),
        {e.endpoint_conn(stem, True, NAMES_C)},
        .fills_done(cu_f[{i}]), .gemms_done(cu_g[{i}]), .drains_done(cu_d[{i}])
    );""")

    # ---- vector cores --------------------------------------------------
    NAMES_V = (
        "noc_in_data",
        "noc_in_valid",
        "noc_in_busy",
        "noc_out_data",
        "noc_out_valid",
        "noc_out_busy",
    )
    for i, (x, y) in enumerate(m.vecs):
        if m._interior(x, y):
            stem, drives = e.link(f"L{x}_{y}"), True
        else:
            stem, drives = e.edge_link(x, y)
        mmx, mmy = m.nearest_mag(x, y)
        e.body.append(
            f"""    vec_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X({x}), .POS_Y({y}),
             .MEM_X({mmx}), .MEM_Y({mmy}), .MODEL(MODEL),
             .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH),
             .L1_DEPTH(L1_DEPTH), .L1_PRIM("block")) u_vec{i} (
        .clk(clk), .resetn(resetn),
        {e.endpoint_conn(stem, drives, NAMES_V)},
        .dbg_cycles(vc_cyc[{i}]), .dbg_fault(vc_flt[{i}])
    );"""
        )

    # ---- tie off every link nothing claimed ----------------------------
    # A link has one driver per direction. Where the endpoint is absent, the
    # direction IT would have driven has none -- data and valid float, and so
    # does the busy coming back at it. Which direction that is depends on which
    # side of the router the link sits on, so it is derived, not assumed.
    used = set()
    for x, y in m.mags + m.vecs:
        used.add(e.link(f"L{x}_{y}") if m._interior(x, y) else e.edge_link(x, y)[0])
    for x, y in m.cus:
        used.add(e.link(f"L{x}_{y}"))

    for key, stem in sorted(e.links.items(), key=lambda kv: kv[1]):
        if stem in used or _internal(key, m):
            continue
        fwd = _absent_drives_fwd(key)
        d, b = ("f", "r") if fwd else ("r", "f")
        e.body.append(
            f"    assign {stem}_{d}d = {{FW{{1'b0}}}};"
            f" assign {stem}_{d}v = 1'b0;"
            f" assign {stem}_{b}b = 1'b0;"
        )

    return render(m, e, name, nm)


def _absent_drives_fwd(key):
    """Would the endpoint on this link have driven `fwd`?

    Locals and the west/north edges are read by the router as `fwd`; the
    east/south edges are driven by it, so their endpoint drives `rev`.
    """
    if key.startswith("L"):
        return True
    kind = key[0]
    a, b = (int(v) for v in key[1:].split("_"))
    return a == 0 if kind == "h" else b == 0


def _internal(key, m):
    """True if this horizontal/vertical link joins two routers rather than an edge."""
    kind, rest = key[0], key[1:]
    a, b = (int(v) for v in rest.split("_"))
    if kind == "h":
        return 1 <= a <= m.nx - 1
    return 1 <= b <= m.ny - 1


def master_names(nmag):
    """Interface names for MAG's masters, in the order mag.v packs them.

    Ports 0..nmag-1 are the memory engines, then the host upload path, then the
    memory mover -- mag.v's MV is MEM_PORTS+1.
    """
    return [f"M_AXI_MEM{i}" for i in range(nmag)] + ["M_AXI_UPLOAD", "M_AXI_MOVER"]


# Each master gets its OWN named interface rather than a slice of one packed
# bus. Vivado infers an AXI interface per name; a packed bus would arrive in the
# block design as loose wires needing Slice IP on every field.
MASTER_FIELDS = [
    ("awid", "IDW", "o"),
    ("awaddr", "AW", "o"),
    ("awlen", "8", "o"),
    ("awsize", "3", "o"),
    ("awburst", "2", "o"),
    ("awvalid", "1", "o"),
    ("awready", "1", "i"),
    ("wdata", "DW", "o"),
    ("wstrb", "DW/8", "o"),
    ("wlast", "1", "o"),
    ("wvalid", "1", "o"),
    ("wready", "1", "i"),
    ("bid", "IDW", "i"),
    ("bresp", "2", "i"),
    ("bvalid", "1", "i"),
    ("bready", "1", "o"),
    ("arid", "IDW", "o"),
    ("araddr", "AW", "o"),
    ("arlen", "8", "o"),
    ("arsize", "3", "o"),
    ("arburst", "2", "o"),
    ("arvalid", "1", "o"),
    ("arready", "1", "i"),
    ("rid", "IDW", "i"),
    ("rdata", "DW", "i"),
    ("rresp", "2", "i"),
    ("rlast", "1", "i"),
    ("rvalid", "1", "i"),
    ("rready", "1", "o"),
]


def master_decls(names):
    """Port declarations for every master interface."""
    out = []
    for n in names:
        out.append(f"    // ---- {n} ----")
        for f, w, d in MASTER_FIELDS:
            kind = "output wire" if d == "o" else "input  wire"
            rng = "" if w == "1" else f"[{w}-1:0] "
            out.append(f"    {kind} {rng}{n}_{f},")
    return "\n".join(out)


def master_glue(names):
    """Wire the packed bus MAG drives out to the individually-named ports."""
    out = []
    for i, n in enumerate(names):
        for f, w, d in MASTER_FIELDS:
            if w == "1":
                lo, sl = f"[{i}]", f"[{i}]"
            else:
                lo, sl = f"[{i}*{w} +: {w}]", f"[{i}*{w} +: {w}]"
            if d == "o":
                out.append(f"    assign {n}_{f} = mm_{f}{lo};")
            else:
                out.append(f"    assign mm_{f}{sl} = {n}_{f};")
    return "\n".join(out)


def master_wires():
    """The packed bus between MAG and the fan-out."""
    out = []
    for f, w, d in MASTER_FIELDS:
        # A one-bit field is still one bit PER MASTER, so the packed wire is
        # [NM-1:0] and not a scalar -- the fan-out indexes every one of them.
        rng = "[NM-1:0] " if w == "1" else f"[NM*{w}-1:0] "
        out.append(f"    wire {rng}mm_{f};")
    return "\n".join(out)


MAG_AXI = """        .sm_awid(S_AXI_MEM_awid), .sm_awaddr(S_AXI_MEM_awaddr),
        .sm_awlen(S_AXI_MEM_awlen), .sm_awvalid(S_AXI_MEM_awvalid),
        .sm_awready(S_AXI_MEM_awready),
        .sm_wdata(S_AXI_MEM_wdata), .sm_wstrb(S_AXI_MEM_wstrb),
        .sm_wlast(S_AXI_MEM_wlast), .sm_wvalid(S_AXI_MEM_wvalid),
        .sm_wready(S_AXI_MEM_wready),
        .sm_bid(S_AXI_MEM_bid), .sm_bresp(S_AXI_MEM_bresp),
        .sm_bvalid(S_AXI_MEM_bvalid), .sm_bready(S_AXI_MEM_bready),
        .sm_arid(S_AXI_MEM_arid), .sm_araddr(S_AXI_MEM_araddr),
        .sm_arlen(S_AXI_MEM_arlen), .sm_arvalid(S_AXI_MEM_arvalid),
        .sm_arready(S_AXI_MEM_arready),
        .sm_rid(S_AXI_MEM_rid), .sm_rdata(S_AXI_MEM_rdata),
        .sm_rresp(S_AXI_MEM_rresp), .sm_rlast(S_AXI_MEM_rlast),
        .sm_rvalid(S_AXI_MEM_rvalid), .sm_rready(S_AXI_MEM_rready),
        .sc_awid(S_AXI_CTRL_awid), .sc_awaddr(S_AXI_CTRL_awaddr),
        .sc_awlen(S_AXI_CTRL_awlen), .sc_awvalid(S_AXI_CTRL_awvalid),
        .sc_awready(S_AXI_CTRL_awready),
        .sc_wdata(S_AXI_CTRL_wdata), .sc_wstrb(S_AXI_CTRL_wstrb),
        .sc_wlast(S_AXI_CTRL_wlast), .sc_wvalid(S_AXI_CTRL_wvalid),
        .sc_wready(S_AXI_CTRL_wready),
        .sc_bid(S_AXI_CTRL_bid), .sc_bresp(S_AXI_CTRL_bresp),
        .sc_bvalid(S_AXI_CTRL_bvalid), .sc_bready(S_AXI_CTRL_bready),
        .sc_arid(S_AXI_CTRL_arid), .sc_araddr(S_AXI_CTRL_araddr),
        .sc_arlen(S_AXI_CTRL_arlen), .sc_arvalid(S_AXI_CTRL_arvalid),
        .sc_arready(S_AXI_CTRL_arready),
        .sc_rid(S_AXI_CTRL_rid), .sc_rdata(S_AXI_CTRL_rdata),
        .sc_rresp(S_AXI_CTRL_rresp), .sc_rlast(S_AXI_CTRL_rlast),
        .sc_rvalid(S_AXI_CTRL_rvalid), .sc_rready(S_AXI_CTRL_rready),
        .m_awid(mm_awid), .m_awaddr(mm_awaddr), .m_awlen(mm_awlen),
        .m_awsize(mm_awsize), .m_awburst(mm_awburst),
        .m_awvalid(mm_awvalid), .m_awready(mm_awready),
        .m_wdata(mm_wdata), .m_wstrb(mm_wstrb), .m_wlast(mm_wlast),
        .m_wvalid(mm_wvalid), .m_wready(mm_wready),
        .m_bid(mm_bid), .m_bresp(mm_bresp), .m_bvalid(mm_bvalid), .m_bready(mm_bready),
        .m_arid(mm_arid), .m_araddr(mm_araddr), .m_arlen(mm_arlen),
        .m_arsize(mm_arsize), .m_arburst(mm_arburst),
        .m_arvalid(mm_arvalid), .m_arready(mm_arready),
        .m_rid(mm_rid), .m_rdata(mm_rdata), .m_rresp(mm_rresp),
        .m_rlast(mm_rlast), .m_rvalid(mm_rvalid), .m_rready(mm_rready),"""


def render(m, e, name, nm):
    picture = "\n".join("//   " + " ".join(r) for r in m.rows)
    ncu, nvec = len(m.cus), len(m.vecs)
    mnames = master_names(len(m.mags))
    ASSOC = ":".join(["S_AXI_MEM", "S_AXI_CTRL"] + mnames)
    MASTER_DECLS = master_decls(mnames).rstrip(",")
    MASTER_WIRES = master_wires()
    MASTER_GLUE = master_glue(mnames)
    return f"""// {name} -- GENERATED by scripts/py/gen_mesh.py. Do not edit by hand.
//
{picture}
//
// {m.nx}x{m.ny} routers, {len(m.mags)} MAG port(s), {ncu} cluster(s), {nvec} vector core(s).
// GRID_X_HI={m.nx} GRID_Y_HI={m.ny}: the clamp is per axis, so the grid need not
// be square. Edge endpoints live outside the router grid and are reached by it.
//
// MAG presents MEM_PORTS+2 = {nm} AXI masters -- one per memory port, plus the
// host upload and the memory mover. Merge them with one SmartConnect in the
// block design; hand-written arbitration here would duplicate it badly.

`default_nettype none

module {name} #(
    parameter integer FW       = 288,
    parameter integer PW       = 4,
    parameter integer DW       = 256,
    parameter integer AW       = 34,
    parameter integer IDW      = 4,
    parameter integer NM       = {nm},
    parameter integer MODEL    = 0,
    parameter integer L1_DEPTH = 512,
    // src/ktpu/hw/bench.py's TILES / L1_A_ENTRIES / L1_B_ENTRIES. A generated
    // top that omits them elaborates mx_cluster_cu's 256/32/32 instead.
    parameter integer TILES    = 512,
    // 512/512, NOT the compiler's 128/256: capacity is an upper bound the
    // planner need not fill, and at 928 bits wide the BRAM cost is set by
    // WIDTH -- 13 RAMB36 per port at any depth to 512 -- so the extra is free.
    // 512 needs the L1 BANK BIT, because aoff/boff are 8-bit fields: the
    // address is the bank concatenated with the offset, and the bank is
    // CU_INST [114]/[113]/[112] -- docs/isa/cluster.md s4.6. Two banks is the
    // ceiling; deeper needs a second bit, which mx_cluster_mgr reports.
    parameter integer GA       = 512,
    parameter integer GB       = 512,
    // MUST be "block" at these depths: 512 x 928 bits of LUTRAM per port is
    // ~100k LUT per cluster. The two parameters are not independent.
    parameter         L1_PRIM  = "block",
    // 512 BECAUSE THE DEPTH IS ALREADY PAID FOR. A 288-bit FIFO is 4 RAMB36 at
    // any depth to 512, so the old 32/64 used 6%/12% of their own tiles.
    // Measured on noc_cu_base: 8 BRAM either way, +34 LUT, 574 -> 567 MHz.
    parameter integer INST_DEPTH = 512,
    parameter integer RECV_DEPTH = 512
)(
    // One clock and one reset for every interface, master and slave alike, so
    // neither carries an s_ or m_ prefix. ASSOCIATED_BUSIF names them all, which
    // is what makes the block design tie them up on its own.
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 axi_aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF {ASSOC}, ASSOCIATED_RESET axi_aresetn" *)
    input  wire axi_aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 axi_aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire axi_aresetn,

    input  wire [IDW-1:0]   S_AXI_MEM_awid,
    input  wire [AW-1:0]    S_AXI_MEM_awaddr,
    input  wire [7:0]       S_AXI_MEM_awlen,
    input  wire             S_AXI_MEM_awvalid,
    output wire             S_AXI_MEM_awready,
    input  wire [DW-1:0]    S_AXI_MEM_wdata,
    input  wire [DW/8-1:0]  S_AXI_MEM_wstrb,
    input  wire             S_AXI_MEM_wlast,
    input  wire             S_AXI_MEM_wvalid,
    output wire             S_AXI_MEM_wready,
    output wire [IDW-1:0]   S_AXI_MEM_bid,
    output wire [1:0]       S_AXI_MEM_bresp,
    output wire             S_AXI_MEM_bvalid,
    input  wire             S_AXI_MEM_bready,
    input  wire [IDW-1:0]   S_AXI_MEM_arid,
    input  wire [AW-1:0]    S_AXI_MEM_araddr,
    input  wire [7:0]       S_AXI_MEM_arlen,
    input  wire             S_AXI_MEM_arvalid,
    output wire             S_AXI_MEM_arready,
    output wire [IDW-1:0]   S_AXI_MEM_rid,
    output wire [DW-1:0]    S_AXI_MEM_rdata,
    output wire [1:0]       S_AXI_MEM_rresp,
    output wire             S_AXI_MEM_rlast,
    output wire             S_AXI_MEM_rvalid,
    input  wire             S_AXI_MEM_rready,

    input  wire [IDW-1:0]   S_AXI_CTRL_awid,
    input  wire [31:0]      S_AXI_CTRL_awaddr,
    input  wire [7:0]       S_AXI_CTRL_awlen,
    input  wire             S_AXI_CTRL_awvalid,
    output wire             S_AXI_CTRL_awready,
    input  wire [63:0]      S_AXI_CTRL_wdata,
    input  wire [7:0]       S_AXI_CTRL_wstrb,
    input  wire             S_AXI_CTRL_wlast,
    input  wire             S_AXI_CTRL_wvalid,
    output wire             S_AXI_CTRL_wready,
    output wire [IDW-1:0]   S_AXI_CTRL_bid,
    output wire [1:0]       S_AXI_CTRL_bresp,
    output wire             S_AXI_CTRL_bvalid,
    input  wire             S_AXI_CTRL_bready,
    input  wire [IDW-1:0]   S_AXI_CTRL_arid,
    input  wire [31:0]      S_AXI_CTRL_araddr,
    input  wire [7:0]       S_AXI_CTRL_arlen,
    input  wire             S_AXI_CTRL_arvalid,
    output wire             S_AXI_CTRL_arready,
    output wire [IDW-1:0]   S_AXI_CTRL_rid,
    output wire [63:0]      S_AXI_CTRL_rdata,
    output wire [1:0]       S_AXI_CTRL_rresp,
    output wire             S_AXI_CTRL_rlast,
    output wire             S_AXI_CTRL_rvalid,
    input  wire             S_AXI_CTRL_rready,

{MASTER_DECLS}
);
    localparam integer NMAG = {len(m.mags)};
    localparam integer NCU  = {ncu};
    localparam integer NVEC = {nvec};

    // The instances below are written against clk/resetn; aliasing once here
    // keeps the generator's body independent of the boundary's naming.
    wire clk    = axi_aclk;
    wire resetn = axi_aresetn;
    wire rst    = !resetn;

{MASTER_WIRES}

{MASTER_GLUE}

    wire [NMAG*FW-1:0] mag_o_d;
    wire [NMAG-1:0]    mag_i_v, mag_i_b, mag_o_v, mag_o_b;
    wire [15:0]        mag_rd, mag_wr;

    wire [15:0] cu_f [0:NCU-1];
    wire [15:0] cu_g [0:NCU-1];
    wire [15:0] cu_d [0:NCU-1];
    wire [31:0] vc_cyc [0:NVEC-1];
    wire        vc_flt [0:NVEC-1];

    // The mover is commanded through S_AXI_CTRL, so its status is internal and
    // reaches `obs` only to stop synthesis pruning it.
    wire        mv_busy;
    wire [3:0]  mv_fault;
    wire [31:0] mv_done;

{chr(10).join(e.decls)}

{chr(10).join(e.body)}

    // The boundary is clock, reset and AXI. There is deliberately no `obs`
    // fold: it existed to stop synthesis pruning these counters, which no
    // software can read, so it was keeping dead logic alive and inflating
    // every measurement taken through it. Pruning them is the right answer.

endmodule

`default_nettype wire
"""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("map", help="the token picture")
    ap.add_argument("-o", "--out", help="output .v (default: stdout)")
    ap.add_argument("-m", "--module", default="ktpu_mesh", help="module name")
    args = ap.parse_args()

    try:
        mesh = Mesh(parse_map(Path(args.map).read_text(encoding="utf-8")))
        text = emit(mesh, args.module)
    except MeshError as exc:
        sys.exit(f"gen_mesh: {exc}")

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(
            f"{args.out}: {mesh.nx}x{mesh.ny} routers, {len(mesh.mags)} mag, "
            f"{len(mesh.cus)} cu, {len(mesh.vecs)} vec, "
            f"{len(mesh.mags) + 2} AXI masters"
        )
    else:
        print(text)


if __name__ == "__main__":
    main()
