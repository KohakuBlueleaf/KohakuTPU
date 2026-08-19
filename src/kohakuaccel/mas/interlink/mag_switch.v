// mag_switch -- the three-port switch inside MAG: link0, link1, local.
//
// docs/interlink/topology.md s2. A SECOND routing layer, so it needs its own
// deadlock proof, and the proof is the shape: SLR0..SLR3 hold mesh 0, 1, 3, 2
// and an SLL joins only ADJACENT SLRs, so the three buildable links are
//
//        mesh0 ── mesh1 ── mesh3 ── mesh2      link0 is the lower neighbour
//        pos 0    pos 1    pos 2    pos 3      link1 is the higher one
//
// and mesh0 to mesh2 is three hops. ROUTING IS ONE COMPARISON -- move one
// position toward the destination -- so position is monotone along a path and a
// packet never reverses. The channel dependency graph is then two disjoint
// chains, each upward channel depending only on the next upward one and
// likewise downward: acyclic, hence deadlock-free. A RING would close it.
//
// The two classes stay separate all the way to the link, because merging them
// on the transmit side puts a stalled forward in front of traffic that was
// going to terminate anyway.
//
// TRANSIT AND LOCAL MERGE ROUND-ROBIN, not transit-first. Local egress is one
// queue, so strict priority lets a saturated through-stream pin its head and
// stop the mesh injecting in either direction; round-robin bounds transit's
// wait at one local packet, which is all the proof above needs.
//
// mesh0 has no lower neighbour and mesh2 no higher one. Their unused port is
// tied off outside, and a packet arriving there still needing a forward is a
// fault -- the routing above cannot produce one.

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

    // ---- link 0: the neighbour one position DOWN the chain ----------------
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

    // ---- link 1: the neighbour one position UP ----------------------------
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

    localparam integer F_NOFWD  = 0,   // a transit packet with no onward link
                       F_SELF   = 1,   // local egress addressed to this mesh
                       F_PKTLEN = 2;   // a packet no credit can ever cover

    // The chain, low bits first: position 0 is mesh0, position 3 is mesh2. This
    // is the only place the SLR order is written.
    localparam [7:0] CH_SEQ = 8'b10_11_01_00;

    function [1:0] ch_pos;
        input [1:0] m;
        integer p;
        begin
            ch_pos = 2'd0;
            for (p = 0; p < 4; p = p + 1)
                if (CH_SEQ[2*p +: 2] == m) ch_pos = p[1:0];
        end
    endfunction

    wire [1:0] my_pos = ch_pos(my_mesh);
    wire       has0   = (my_pos != 2'd0);
    wire       has1   = (my_pos != 2'd3);

    // Derived, not configured: a separately configured peer id is a second place
    // for the topology to be wrong, and the two would disagree in silence.
    // At an end node the missing peer's index wraps and reads a neighbour that
    // is not there; `has0`/`has1` gate every use of it.
    wire [1:0] peer0 = CH_SEQ[{my_pos - 2'd1, 1'b0} +: 2];
    wire [1:0] peer1 = CH_SEQ[{my_pos + 2'd1, 1'b0} +: 2];

    wire [TUSER_W-1:0] l0r0h, l0r1h, l1r0h, l1r1h;
    wire [LINK_W-1:0]  l0r0d, l0r1d, l1r0d, l1r1d;
    wire l0r0hv, l0r1hv, l1r0hv, l1r1hv;
    wire l0r0dv, l0r1dv, l1r0dv, l1r1dv;
    wire l0r0dl, l0r1dl, l1r0dl, l1r1dl;
    wire l0r0hr, l0r1hr, l1r0hr, l1r1hr;
    wire l0r0dr, l0r1dr, l1r0dr, l1r1dr;

    wire [TUSER_W-1:0] dm_hdr, f0_hdr, f1_hdr;
    wire [LINK_W-1:0]  dm_dat, f0_dat, f1_dat;
    wire [3:0] dm_hv, dm_hr, dm_dv, dm_dr;
    wire [1:0] f0_hv, f0_hr, f0_dv, f0_dr;
    wire [1:0] f1_hv, f1_hr, f1_dv, f1_dr;
    wire dm_dl, f0_dl, f1_dl;
    wire [1:0] flen;
    reg  [2:0] fault_r;

    wire [TUSER_W-1:0] t00_hdr, t01_hdr, t10_hdr, t11_hdr;
    wire [LINK_W-1:0]  t00_dat, t01_dat, t10_dat, t11_dat;
    wire t00_hv, t00_hr, t00_dv, t00_dr, t00_dl;
    wire t01_hv, t01_hr, t01_dv, t01_dr, t01_dl;
    wire t10_hv, t10_hr, t10_dv, t10_dr, t10_dl;
    wire t11_hv, t11_hr, t11_dv, t11_dr, t11_dl;

    // =====================================================================
    // Local egress: pick the direction, then the class on that link. Output 4
    // is the sink for a packet already at its destination.
    // =====================================================================
    wire [1:0] l_dst  = ltx_hdr[U_DMESH +: 2];
    wire [1:0] l_pos  = ch_pos(l_dst);
    wire       l_self = (l_dst == my_mesh);
    wire       l_up   = (l_pos > my_pos);
    wire       l_cls  = l_up ? (l_dst != peer1) : (l_dst != peer0);

    wire [2:0] l_sel = l_self ? 3'd4 : {1'b0, l_up, l_cls};

    il_pkt_demux #(.LINK_W(LINK_W), .TUSER_W(TUSER_W), .N_OUT(4)) u_ldm (
        .clk(clk), .resetn(resetn), .sel_in(l_sel),
        .i_hdr(ltx_hdr), .i_hvalid(ltx_hvalid), .i_hready(ltx_hready),
        .i_dat(ltx_dat), .i_dlast(ltx_dlast), .i_dvalid(ltx_dvalid),
        .i_dready(ltx_dready),
        .o_hdr(dm_hdr), .o_hvalid(dm_hv), .o_hready(dm_hr),
        .o_dat(dm_dat), .o_dlast(dm_dl), .o_dvalid(dm_dv), .o_dready(dm_dr),
        .dropped()
    );

    // =====================================================================
    // Transit. A packet that arrived on one link and is not for this mesh
    // continues on the other, keeping its direction; only its class is decided
    // here. Output 2 is the sink for an end node, where there is no other link.
    // =====================================================================
    wire [1:0] f0_dst = l0r1h[U_DMESH +: 2];
    wire [1:0] f1_dst = l1r1h[U_DMESH +: 2];
    wire [1:0] f0_sel = has1 ? {1'b0, f0_dst != peer1} : 2'd2;
    wire [1:0] f1_sel = has0 ? {1'b0, f1_dst != peer0} : 2'd2;

    il_pkt_demux #(.LINK_W(LINK_W), .TUSER_W(TUSER_W), .N_OUT(2)) u_f0 (
        .clk(clk), .resetn(resetn), .sel_in(f0_sel),
        .i_hdr(l0r1h), .i_hvalid(l0r1hv), .i_hready(l0r1hr),
        .i_dat(l0r1d), .i_dlast(l0r1dl), .i_dvalid(l0r1dv), .i_dready(l0r1dr),
        .o_hdr(f0_hdr), .o_hvalid(f0_hv), .o_hready(f0_hr),
        .o_dat(f0_dat), .o_dlast(f0_dl), .o_dvalid(f0_dv), .o_dready(f0_dr),
        .dropped()
    );

    il_pkt_demux #(.LINK_W(LINK_W), .TUSER_W(TUSER_W), .N_OUT(2)) u_f1 (
        .clk(clk), .resetn(resetn), .sel_in(f1_sel),
        .i_hdr(l1r1h), .i_hvalid(l1r1hv), .i_hready(l1r1hr),
        .i_dat(l1r1d), .i_dlast(l1r1dl), .i_dvalid(l1r1dv), .i_dready(l1r1dr),
        .o_hdr(f1_hdr), .o_hvalid(f1_hv), .o_hready(f1_hr),
        .o_dat(f1_dat), .o_dlast(f1_dl), .o_dvalid(f1_dv), .o_dready(f1_dr),
        .dropped()
    );

    // =====================================================================
    // One merge per link per class: this mesh's own traffic against the
    // transit stream heading the same way.
    // =====================================================================
    il_pkt_mux2 #(.LINK_W(LINK_W), .TUSER_W(TUSER_W)) u_t00 (
        .clk(clk), .resetn(resetn),
        .a_hdr(dm_hdr), .a_hvalid(dm_hv[0]), .a_hready(dm_hr[0]),
        .a_dat(dm_dat), .a_dlast(dm_dl), .a_dvalid(dm_dv[0]), .a_dready(dm_dr[0]),
        .b_hdr(f1_hdr), .b_hvalid(f1_hv[0]), .b_hready(f1_hr[0]),
        .b_dat(f1_dat), .b_dlast(f1_dl), .b_dvalid(f1_dv[0]), .b_dready(f1_dr[0]),
        .o_hdr(t00_hdr), .o_hvalid(t00_hv), .o_hready(t00_hr),
        .o_dat(t00_dat), .o_dlast(t00_dl), .o_dvalid(t00_dv), .o_dready(t00_dr)
    );

    il_pkt_mux2 #(.LINK_W(LINK_W), .TUSER_W(TUSER_W)) u_t01 (
        .clk(clk), .resetn(resetn),
        .a_hdr(dm_hdr), .a_hvalid(dm_hv[1]), .a_hready(dm_hr[1]),
        .a_dat(dm_dat), .a_dlast(dm_dl), .a_dvalid(dm_dv[1]), .a_dready(dm_dr[1]),
        .b_hdr(f1_hdr), .b_hvalid(f1_hv[1]), .b_hready(f1_hr[1]),
        .b_dat(f1_dat), .b_dlast(f1_dl), .b_dvalid(f1_dv[1]), .b_dready(f1_dr[1]),
        .o_hdr(t01_hdr), .o_hvalid(t01_hv), .o_hready(t01_hr),
        .o_dat(t01_dat), .o_dlast(t01_dl), .o_dvalid(t01_dv), .o_dready(t01_dr)
    );

    il_pkt_mux2 #(.LINK_W(LINK_W), .TUSER_W(TUSER_W)) u_t10 (
        .clk(clk), .resetn(resetn),
        .a_hdr(dm_hdr), .a_hvalid(dm_hv[2]), .a_hready(dm_hr[2]),
        .a_dat(dm_dat), .a_dlast(dm_dl), .a_dvalid(dm_dv[2]), .a_dready(dm_dr[2]),
        .b_hdr(f0_hdr), .b_hvalid(f0_hv[0]), .b_hready(f0_hr[0]),
        .b_dat(f0_dat), .b_dlast(f0_dl), .b_dvalid(f0_dv[0]), .b_dready(f0_dr[0]),
        .o_hdr(t10_hdr), .o_hvalid(t10_hv), .o_hready(t10_hr),
        .o_dat(t10_dat), .o_dlast(t10_dl), .o_dvalid(t10_dv), .o_dready(t10_dr)
    );

    il_pkt_mux2 #(.LINK_W(LINK_W), .TUSER_W(TUSER_W)) u_t11 (
        .clk(clk), .resetn(resetn),
        .a_hdr(dm_hdr), .a_hvalid(dm_hv[3]), .a_hready(dm_hr[3]),
        .a_dat(dm_dat), .a_dlast(dm_dl), .a_dvalid(dm_dv[3]), .a_dready(dm_dr[3]),
        .b_hdr(f0_hdr), .b_hvalid(f0_hv[1]), .b_hready(f0_hr[1]),
        .b_dat(f0_dat), .b_dlast(f0_dl), .b_dvalid(f0_dv[1]), .b_dready(f0_dr[1]),
        .o_hdr(t11_hdr), .o_hvalid(t11_hv), .o_hready(t11_hr),
        .o_dat(t11_dat), .o_dlast(t11_dl), .o_dvalid(t11_dv), .o_dready(t11_dr)
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
        .tx0_hdr(t00_hdr), .tx0_hvalid(t00_hv), .tx0_hready(t00_hr),
        .tx0_dat(t00_dat), .tx0_dlast(t00_dl), .tx0_dvalid(t00_dv),
        .tx0_dready(t00_dr),
        .tx1_hdr(t01_hdr), .tx1_hvalid(t01_hv), .tx1_hready(t01_hr),
        .tx1_dat(t01_dat), .tx1_dlast(t01_dl), .tx1_dvalid(t01_dv),
        .tx1_dready(t01_dr),
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

    mag_link #(.LINK_W(LINK_W), .TUSER_W(TUSER_W), .RX_BEATS(RX_BEATS),
               .CRED_BATCH(CRED_BATCH), .MAX_BEATS(MAX_BEATS)) u_l1 (
        .clk(clk), .resetn(resetn), .my_mesh(my_mesh), .peer_mesh(peer1),
        .tx0_hdr(t10_hdr), .tx0_hvalid(t10_hv), .tx0_hready(t10_hr),
        .tx0_dat(t10_dat), .tx0_dlast(t10_dl), .tx0_dvalid(t10_dv),
        .tx0_dready(t10_dr),
        .tx1_hdr(t11_hdr), .tx1_hvalid(t11_hv), .tx1_hready(t11_hr),
        .tx1_dat(t11_dat), .tx1_dlast(t11_dl), .tx1_dvalid(t11_dv),
        .tx1_dready(t11_dr),
        .rx0_hdr(l1r0h), .rx0_hvalid(l1r0hv), .rx0_hready(l1r0hr),
        .rx0_dat(l1r0d), .rx0_dlast(l1r0dl), .rx0_dvalid(l1r0dv),
        .rx0_dready(l1r0dr),
        .rx1_hdr(l1r1h), .rx1_hvalid(l1r1hv), .rx1_hready(l1r1hr),
        .rx1_dat(l1r1d), .rx1_dlast(l1r1dl), .rx1_dvalid(l1r1dv),
        .rx1_dready(l1r1dr),
        .m_axis_tdata(m1_tdata), .m_axis_tuser(m1_tuser), .m_axis_tlast(m1_tlast),
        .m_axis_tvalid(m1_tvalid), .m_axis_tready(m1_tready),
        .s_axis_tdata(s1_tdata), .s_axis_tuser(s1_tuser), .s_axis_tlast(s1_tlast),
        .s_axis_tvalid(s1_tvalid), .s_axis_tready(s1_tready),
        .ctr_tx(ctr_tx1), .ctr_rx(ctr_rx1), .ctr_stall(ctr_stall1),
        .cred_state(cred1_state), .fault_len(flen[1])
    );

    // =====================================================================
    wire no_fwd = (l0r1hv && !has1) || (l1r1hv && !has0);

    always @(posedge clk) begin
        if (!resetn) fault_r <= 3'd0;
        else begin
            if (no_fwd)               fault_r[F_NOFWD]  <= 1'b1;
            if (ltx_hvalid && l_self) fault_r[F_SELF]   <= 1'b1;
            if (|flen)                fault_r[F_PKTLEN] <= 1'b1;
        end
    end
    assign fault = {1'b0, fault_r};

    wire fwd_go  = (l0r1hv && l0r1hr) || (l1r1hv && l1r1hr);
    wire fwd_blk = (l0r1hv && !l0r1hr) || (l1r1hv && !l1r1hr);

    reg [31:0] n_fwd, n_fwd_blk, n_lblk;
    always @(posedge clk) begin
        if (!resetn) begin
            n_fwd <= 32'd0; n_fwd_blk <= 32'd0; n_lblk <= 32'd0;
        end else begin
            if (fwd_go)                     n_fwd     <= n_fwd + 32'd1;
            if (fwd_blk)                    n_fwd_blk <= n_fwd_blk + 32'd1;
            if (ltx_hvalid && !ltx_hready)  n_lblk    <= n_lblk + 32'd1;
        end
    end
    assign ctr_fwd    = {n_fwd_blk, n_fwd};
    assign ctr_lblock = {32'd0, n_lblk};

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        if (no_fwd)
            $display("%0t ERROR mag_switch: mesh %0d is at the end of the chain and a packet arrived needing to be forwarded past it. Either my_mesh is wrong or a sender routed by something other than chain order.",
                     $time, my_mesh);
        if (ltx_hvalid && l_self)
            $display("%0t ERROR mag_switch: local egress addressed to mesh %0d, which is this mesh -- mag_ilink should have kept it local. Dropped.",
                     $time, my_mesh);
    end
`endif

endmodule

`default_nettype wire
