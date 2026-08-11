// mag_switch -- the three-port switch inside MAG: link0, link1, local.
//
// docs/interlink/topology.md s2. This is a SECOND routing layer and it does not
// inherit the NoC's deadlock proof. It gets its own, by the same argument:
// XY dimension-order on MESH coordinates over a rectangular grid.
//
//   mesh id 0..3 is (x, y) = (id[0], id[1]):
//
//        (0,0) mesh0 ── mesh1 (1,0)      link0 is the X neighbour
//          │              │              link1 is the Y neighbour
//        (0,1) mesh2 ── mesh3 (1,1)
//
// X first, then Y. Two consequences, both load-bearing:
//
//   * a forwarded packet ALWAYS turns X into Y, never Y into X. link0's forward
//     class feeds link1, and link1's forward class is provably dead -- traffic
//     there is the turn the model forbids, so it is a fault and not a case.
//   * the channel dependency graph is X -> Y and nothing else. Acyclic, hence
//     deadlock-free, and only while the mesh-of-meshes stays a GRID.
//
// TWO LINKS, NOT NLINK. The mesh id is two bits (.plan/decisions.md), so a fifth
// mesh is an ISA change rather than a parameter change, and a port count that
// cannot vary should not be spelled as though it can.
//
// Local egress is ONE queue, so a packet held on a stalled link blocks one
// behind it bound elsewhere. Counted rather than fixed: v1 traffic is a pipeline
// stage boundary, which is nearly all one destination.

`default_nettype none

module mag_switch #(
    parameter integer LINK_W     = 288,
    parameter integer TUSER_W    = 96,
    parameter integer RX_BEATS   = 64,
    parameter integer CRED_BATCH = 8,
    parameter integer MAX_BEATS  = 32
)(
    input  wire                 clk,
    input  wire                 resetn,
    input  wire [1:0]           my_mesh,

    // ---- local egress: a packet for another mesh --------------------------
    input  wire [TUSER_W-1:0]   ltx_hdr,
    input  wire                 ltx_hvalid,
    output wire                 ltx_hready,
    input  wire [LINK_W-1:0]    ltx_dat,
    input  wire                 ltx_dlast,
    input  wire                 ltx_dvalid,
    output wire                 ltx_dready,

    // ---- local ingress: a packet that terminates in this mesh -------------
    output wire [TUSER_W-1:0]   lrx_hdr,
    output wire                 lrx_hvalid,
    input  wire                 lrx_hready,
    output wire [LINK_W-1:0]    lrx_dat,
    output wire                 lrx_dlast,
    output wire                 lrx_dvalid,
    input  wire                 lrx_dready,

    // ---- link 0: the X neighbour ------------------------------------------
    output wire [LINK_W-1:0]    m0_tdata,
    output wire [TUSER_W-1:0]   m0_tuser,
    output wire                 m0_tlast,
    output wire                 m0_tvalid,
    input  wire                 m0_tready,
    input  wire [LINK_W-1:0]    s0_tdata,
    input  wire [TUSER_W-1:0]   s0_tuser,
    input  wire                 s0_tlast,
    input  wire                 s0_tvalid,
    output wire                 s0_tready,

    // ---- link 1: the Y neighbour ------------------------------------------
    output wire [LINK_W-1:0]    m1_tdata,
    output wire [TUSER_W-1:0]   m1_tuser,
    output wire                 m1_tlast,
    output wire                 m1_tvalid,
    input  wire                 m1_tready,
    input  wire [LINK_W-1:0]    s1_tdata,
    input  wire [TUSER_W-1:0]   s1_tuser,
    input  wire                 s1_tlast,
    input  wire                 s1_tvalid,
    output wire                 s1_tready,

    // ---- status -----------------------------------------------------------
    output wire [63:0]          ctr_tx0, ctr_rx0, ctr_stall0,
    output wire [63:0]          ctr_tx1, ctr_rx1, ctr_stall1,
    output wire [63:0]          ctr_fwd,      // {cycles blocked, packets}
    output wire [63:0]          ctr_lblock,   // {0, cycles local egress blocked}
    output wire [31:0]          cred0_state, cred1_state,
    output wire [3:0]           fault
);
    localparam integer U_DMESH = 4, U_LEN = 16;

    localparam integer F_YTURN  = 0,   // a packet asked for the forbidden turn
                       F_SELF   = 1,   // local egress addressed to this mesh
                       F_PKTLEN = 2;   // a packet no credit can ever cover

    // Derived, not configured: one flipped coordinate each. A separately
    // configured peer id is a second place for the topology to be wrong, and
    // the two would disagree in silence.
    wire [1:0] peer0 = {my_mesh[1], ~my_mesh[0]};
    wire [1:0] peer1 = {~my_mesh[1], my_mesh[0]};

    wire [TUSER_W-1:0] l0r0h, l0r1h, l1r0h, l1r1h;
    wire [LINK_W-1:0]  l0r0d, l0r1d, l1r0d, l1r1d;
    wire l0r0hv, l0r1hv, l1r0hv, l1r1hv;
    wire l0r0dv, l0r1dv, l1r0dv, l1r1dv;
    wire l0r0dl, l0r1dl, l1r0dl, l1r1dl;
    wire l0r0hr, l0r1hr, l1r0hr;
    wire l0r0dr, l0r1dr, l1r0dr;

    wire [TUSER_W-1:0] dm_hdr, y_hdr;
    wire [LINK_W-1:0]  dm_dat, y_dat;
    wire [2:0] dm_hv, dm_hr, dm_dv, dm_dr;
    wire dm_dl, dm_drop, y_hv, y_hr, y_dv, y_dr, y_dl;
    wire [1:0] flen;
    reg  [2:0] fault_r;

    // =====================================================================
    // Local egress: X first, then Y. Output 3 of the demux is the sink for a
    // packet addressed at this mesh, which mag_ilink should have kept local.
    // =====================================================================
    wire [1:0] l_dst  = ltx_hdr[U_DMESH +: 2];
    wire       l_useX = (l_dst[0] != my_mesh[0]);
    wire       l_self = (l_dst == my_mesh);
    // Reaching link1 implies the X coordinate already matches, so its peer IS
    // the destination and class 1 there cannot arise.
    wire       l_cls0 = (l_dst != peer0);

    wire [1:0] l_sel = l_self  ? 2'd3 :
                       !l_useX ? 2'd2 :
                       l_cls0  ? 2'd1 : 2'd0;

    il_pkt_demux4 #(.LINK_W(LINK_W), .TUSER_W(TUSER_W)) u_ldm (
        .clk(clk), .resetn(resetn), .sel_in(l_sel),
        .i_hdr(ltx_hdr), .i_hvalid(ltx_hvalid), .i_hready(ltx_hready),
        .i_dat(ltx_dat), .i_dlast(ltx_dlast), .i_dvalid(ltx_dvalid),
        .i_dready(ltx_dready),
        .o_hdr(dm_hdr), .o_hvalid(dm_hv), .o_hready(dm_hr),
        .o_dat(dm_dat), .o_dlast(dm_dl), .o_dvalid(dm_dv), .o_dready(dm_dr),
        .dropped(dm_drop)
    );

    // =====================================================================
    // link1's outbound: local Y traffic and link0's forwarded traffic, merged
    // at packet granularity. Forwarding is cut-through -- a forwarded beat
    // leaves as it arrives rather than after the whole packet has landed.
    // =====================================================================
    il_pkt_mux2 #(.LINK_W(LINK_W), .TUSER_W(TUSER_W)) u_ymux (
        .clk(clk), .resetn(resetn),
        .a_hdr(dm_hdr), .a_hvalid(dm_hv[2]), .a_hready(dm_hr[2]),
        .a_dat(dm_dat), .a_dlast(dm_dl), .a_dvalid(dm_dv[2]), .a_dready(dm_dr[2]),
        .b_hdr(l0r1h), .b_hvalid(l0r1hv), .b_hready(l0r1hr),
        .b_dat(l0r1d), .b_dlast(l0r1dl), .b_dvalid(l0r1dv), .b_dready(l0r1dr),
        .o_hdr(y_hdr), .o_hvalid(y_hv), .o_hready(y_hr),
        .o_dat(y_dat), .o_dlast(y_dl), .o_dvalid(y_dv), .o_dready(y_dr)
    );

    // =====================================================================
    // Local ingress: both links' class 0.
    // =====================================================================
    il_pkt_mux2 #(.LINK_W(LINK_W), .TUSER_W(TUSER_W)) u_lmux (
        .clk(clk), .resetn(resetn),
        .a_hdr(l0r0h), .a_hvalid(l0r0hv), .a_hready(l0r0hr),
        .a_dat(l0r0d), .a_dlast(l0r0dl), .a_dvalid(l0r0dv), .a_dready(l0r0dr),
        .b_hdr(l1r0h), .b_hvalid(l1r0hv), .b_hready(l1r0hr),
        .b_dat(l1r0d), .b_dlast(l1r0dl), .b_dvalid(l1r0dv), .b_dready(l1r0dr),
        .o_hdr(lrx_hdr), .o_hvalid(lrx_hvalid), .o_hready(lrx_hready),
        .o_dat(lrx_dat), .o_dlast(lrx_dlast), .o_dvalid(lrx_dvalid),
        .o_dready(lrx_dready)
    );

    // =====================================================================
    mag_link #(.LINK_W(LINK_W), .TUSER_W(TUSER_W), .RX_BEATS(RX_BEATS),
               .CRED_BATCH(CRED_BATCH), .MAX_BEATS(MAX_BEATS)) u_l0 (
        .clk(clk), .resetn(resetn), .my_mesh(my_mesh), .peer_mesh(peer0),
        .tx0_hdr(dm_hdr), .tx0_hvalid(dm_hv[0]), .tx0_hready(dm_hr[0]),
        .tx0_dat(dm_dat), .tx0_dlast(dm_dl), .tx0_dvalid(dm_dv[0]),
        .tx0_dready(dm_dr[0]),
        .tx1_hdr(dm_hdr), .tx1_hvalid(dm_hv[1]), .tx1_hready(dm_hr[1]),
        .tx1_dat(dm_dat), .tx1_dlast(dm_dl), .tx1_dvalid(dm_dv[1]),
        .tx1_dready(dm_dr[1]),
        .rx0_hdr(l0r0h), .rx0_hvalid(l0r0hv), .rx0_hready(l0r0hr),
        .rx0_dat(l0r0d), .rx0_dlast(l0r0dl), .rx0_dvalid(l0r0dv),
        .rx0_dready(l0r0dr),
        .rx1_hdr(l0r1h), .rx1_hvalid(l0r1hv), .rx1_hready(l0r1hr),
        .rx1_dat(l0r1d), .rx1_dlast(l0r1dl), .rx1_dvalid(l0r1dv),
        .rx1_dready(l0r1dr),
        .m_axis_tdata(m0_tdata), .m_axis_tuser(m0_tuser), .m_axis_tlast(m0_tlast),
        .m_axis_tvalid(m0_tvalid), .m_axis_tready(m0_tready),
        .s_axis_tdata(s0_tdata), .s_axis_tuser(s0_tuser), .s_axis_tlast(s0_tlast),
        .s_axis_tvalid(s0_tvalid), .s_axis_tready(s0_tready),
        .ctr_tx(ctr_tx0), .ctr_rx(ctr_rx0), .ctr_stall(ctr_stall0),
        .cred_state(cred0_state), .fault_len(flen[0])
    );

    // link1's class 1 is drained unconditionally. It cannot receive traffic if
    // the turn model holds, and if it does, backing up would wedge the link
    // instead of reporting which rule was broken.
    mag_link #(.LINK_W(LINK_W), .TUSER_W(TUSER_W), .RX_BEATS(RX_BEATS),
               .CRED_BATCH(CRED_BATCH), .MAX_BEATS(MAX_BEATS)) u_l1 (
        .clk(clk), .resetn(resetn), .my_mesh(my_mesh), .peer_mesh(peer1),
        .tx0_hdr(y_hdr), .tx0_hvalid(y_hv), .tx0_hready(y_hr),
        .tx0_dat(y_dat), .tx0_dlast(y_dl), .tx0_dvalid(y_dv), .tx0_dready(y_dr),
        .tx1_hdr({TUSER_W{1'b0}}), .tx1_hvalid(1'b0), .tx1_hready(),
        .tx1_dat({LINK_W{1'b0}}), .tx1_dlast(1'b0), .tx1_dvalid(1'b0),
        .tx1_dready(),
        .rx0_hdr(l1r0h), .rx0_hvalid(l1r0hv), .rx0_hready(l1r0hr),
        .rx0_dat(l1r0d), .rx0_dlast(l1r0dl), .rx0_dvalid(l1r0dv),
        .rx0_dready(l1r0dr),
        .rx1_hdr(l1r1h), .rx1_hvalid(l1r1hv), .rx1_hready(1'b1),
        .rx1_dat(l1r1d), .rx1_dlast(l1r1dl), .rx1_dvalid(l1r1dv),
        .rx1_dready(1'b1),
        .m_axis_tdata(m1_tdata), .m_axis_tuser(m1_tuser), .m_axis_tlast(m1_tlast),
        .m_axis_tvalid(m1_tvalid), .m_axis_tready(m1_tready),
        .s_axis_tdata(s1_tdata), .s_axis_tuser(s1_tuser), .s_axis_tlast(s1_tlast),
        .s_axis_tvalid(s1_tvalid), .s_axis_tready(s1_tready),
        .ctr_tx(ctr_tx1), .ctr_rx(ctr_rx1), .ctr_stall(ctr_stall1),
        .cred_state(cred1_state), .fault_len(flen[1])
    );

    // =====================================================================
    always @(posedge clk) begin
        if (!resetn) fault_r <= 3'd0;
        else begin
            if (l1r1hv)               fault_r[F_YTURN]  <= 1'b1;
            if (ltx_hvalid && l_self) fault_r[F_SELF]   <= 1'b1;
            if (|flen)                fault_r[F_PKTLEN] <= 1'b1;
        end
    end
    assign fault = {1'b0, fault_r};

    reg [31:0] n_fwd, n_fwd_blk, n_lblk;
    always @(posedge clk) begin
        if (!resetn) begin
            n_fwd <= 32'd0; n_fwd_blk <= 32'd0; n_lblk <= 32'd0;
        end else begin
            if (l0r1hv &&  l0r1hr)          n_fwd     <= n_fwd + 32'd1;
            if (l0r1hv && !l0r1hr)          n_fwd_blk <= n_fwd_blk + 32'd1;
            if (ltx_hvalid && !ltx_hready)  n_lblk    <= n_lblk + 32'd1;
        end
    end
    assign ctr_fwd    = {n_fwd_blk, n_fwd};
    assign ctr_lblock = {32'd0, n_lblk};

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        if (l1r1hv)
            $display("%0t ERROR mag_switch: a packet arrived on link1 needing a forward. XY forbids the Y-to-X turn, so either my_mesh is wrong or a sender routed by something other than XY.",
                     $time);
        if (ltx_hvalid && l_self)
            $display("%0t ERROR mag_switch: local egress addressed to mesh %0d, which is this mesh -- mag_ilink should have kept it local. Dropped.",
                     $time, my_mesh);
    end
`endif

endmodule

`default_nettype wire
