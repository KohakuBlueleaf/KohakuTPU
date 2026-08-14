// The whole partition, driven the way a real driver would drive it.
//
//   fake AXI master (the driver)  ─┐
//                                  ├─ xbar ─┬─ main orchestrator (slave)
//   main orchestrator (master) ────┘        └─ MAG control
//                                                 │
//                                    MAG ─ AXI master ─► axi_ram
//                                                 │
//                                      2 NoC ports ─► 2x2 mesh ─► 2 clusters
//
// WHAT THIS PROVES, that the earlier system benches did not: the host never
// touches the mesh. It writes a CONTROL PROGRAM into the main orchestrator --
// a list of WR / POLL / DONE commands -- and writes GO. The orchestrator then
// drives the whole computation over AXI by itself, including waiting for
// completion, and raises a flag at the end.
//
// That is the real driver contract. A driver over JTAG and a driver over PCIe
// issue the same commands; only the latency of loading them differs, and
// neither is in the loop while the machine runs.
//
//   C[16,16] = A[16,64] x B[64,16],  split by output column across 2 clusters
//   cluster 0 -> C[:, 0:8]     cluster 1 -> C[:, 8:16]
//
// The two clusters compute the SAME numbers by DIFFERENT routes, and the
// expected result is an exact integer, so both halves must match it:
//
//   cluster 0   FP16 in memory, quantised on every read; explicit DRAIN
//   cluster 1   int7 in memory, quantised once on upload; FUSED drain, where
//               each sub-tile leaves on the command that completes it
//
// That is what makes this a test of those paths rather than a demonstration
// that they run. K is two blocks because a fused drain needs a sweep whose
// last K block is not also its first.

`default_nettype none
`timescale 1ns/1ps

`ifndef MX_MODEL
`define MX_MODEL 1
`endif

module mag_system_tb;

    localparam integer FW    = 288;
    localparam integer PW    = 4;
    localparam integer MODEL = `MX_MODEL;
    localparam integer DW    = 256;          // AXI memory width == flit payload

    localparam integer M = 16, N = 16, KK = 64;
    localparam integer GM = M/4;             // 4 row groups
    localparam integer GNC = (N/2)/4;        // 2 column groups per cluster
    localparam integer NKB = KK/32;          // 2 K blocks
    localparam integer NT = GM*GNC;          // 8 sub-tiles per cluster

    // memory word map
    localparam integer SBIAS    = 20;        // E5M3 scale bias, see mx_quant.v
    localparam integer HEADROOM = 8;         // keeps the drained tile in FP16
    localparam integer WPE      = 8;         // FP16 source words per L1 entry
    localparam integer WPQ      = 4;         // int7 words per L1 entry
    localparam integer WA_BASE  = 0;         // 8 entries x 8 = 64 words
    localparam integer WB0_BASE = 64;        // 4 entries x 8 = 32 words
    localparam integer WB1_BASE = 96;
    localparam integer WC0_BASE = 128;
    localparam integer WC1_BASE = 160;
    // Cluster 1's operands, written by the host through the quantising window.
    localparam integer WQA_BASE = 192;       // 8 entries x 4 = 32 words
    localparam integer WQB_BASE = 256;       // 4 entries x 4 = 16 words

    // control address map: bit 28 selects MAG over the main orchestrator
    localparam [31:0] ORC_BASE = 32'h0000_0000;
    localparam [31:0] MAG_BASE = 32'h1000_0000;

    // main orchestrator registers
    localparam [31:0] MO_CTRL = 32'h0000, MO_PC = 32'h0008,
                      MO_CODE = 32'h0010, MO_CMD = 32'h1000;
    localparam [3:0]  OP_WR = 4'd1, OP_POLL = 4'd2, OP_DONE = 4'd3;

    // agent (inside MAG) registers -- the existing orchestrator map
    localparam [31:0] A_PROG_DST = 32'h0040, A_PROG_LEN = 32'h0048,
                      A_PROG_KICK= 32'h0050, A_PROG_CRED= 32'h0060,
                      A_NODE     = 32'h1000, A_STAGE    = 32'h2000;

    reg clk = 0, rstn = 0;
    always #2 clk = ~clk;

    // ============================================================ driver AXI
    reg  [3:0]  d_awid=0, d_arid=0;
    reg  [31:0] d_awaddr=0, d_araddr=0;
    reg  [7:0]  d_awlen=0, d_arlen=0;
    reg         d_awvalid=0, d_arvalid=0, d_wvalid=0, d_wlast=0, d_bready=0, d_rready=0;
    reg  [63:0] d_wdata=0;
    wire        d_awready, d_wready, d_bvalid, d_arready, d_rvalid, d_rlast;
    wire [63:0] d_rdata;
    wire [3:0]  d_bid, d_rid;
    wire [1:0]  d_bresp, d_rresp;

    // main orchestrator master
    wire [3:0]  o_awid, o_arid, o_bid, o_rid;
    wire [31:0] o_awaddr, o_araddr;
    wire [7:0]  o_awlen, o_arlen;
    wire        o_awvalid, o_awready, o_wvalid, o_wready, o_wlast;
    wire        o_bvalid, o_bready, o_arvalid, o_arready, o_rvalid, o_rready, o_rlast;
    wire [63:0] o_wdata, o_rdata;
    wire [7:0]  o_wstrb;
    wire [1:0]  o_bresp, o_rresp;

    // slave 0: main orchestrator regs
    wire [3:0]  s0_awid, s0_arid, s0_bid, s0_rid;
    wire [31:0] s0_awaddr, s0_araddr;
    wire [7:0]  s0_awlen, s0_arlen, s0_wstrb;
    wire        s0_awvalid, s0_awready, s0_wvalid, s0_wready, s0_wlast;
    wire        s0_bvalid, s0_bready, s0_arvalid, s0_arready, s0_rvalid, s0_rready, s0_rlast;
    wire [63:0] s0_wdata, s0_rdata;
    wire [1:0]  s0_bresp, s0_rresp;

    // slave 1: MAG control
    wire [3:0]  s1_awid, s1_arid, s1_bid, s1_rid;
    wire [31:0] s1_awaddr, s1_araddr;
    wire [7:0]  s1_awlen, s1_arlen, s1_wstrb;
    wire        s1_awvalid, s1_awready, s1_wvalid, s1_wready, s1_wlast;
    wire        s1_bvalid, s1_bready, s1_arvalid, s1_arready, s1_rvalid, s1_rready, s1_rlast;
    wire [63:0] s1_wdata, s1_rdata;
    wire [1:0]  s1_bresp, s1_rresp;

    axi_xbar2 #(.ADDR_W(32), .DATA_W(64), .ID_W(4), .SEL_BIT(28)) xbar (
        .clk(clk), .resetn(rstn),
        .m0_awid(d_awid), .m0_awaddr(d_awaddr), .m0_awlen(d_awlen),
        .m0_awvalid(d_awvalid), .m0_awready(d_awready),
        .m0_wdata(d_wdata), .m0_wstrb(8'hFF), .m0_wlast(d_wlast),
        .m0_wvalid(d_wvalid), .m0_wready(d_wready),
        .m0_bid(d_bid), .m0_bresp(d_bresp), .m0_bvalid(d_bvalid), .m0_bready(d_bready),
        .m0_arid(d_arid), .m0_araddr(d_araddr), .m0_arlen(d_arlen),
        .m0_arvalid(d_arvalid), .m0_arready(d_arready),
        .m0_rid(d_rid), .m0_rdata(d_rdata), .m0_rresp(d_rresp),
        .m0_rlast(d_rlast), .m0_rvalid(d_rvalid), .m0_rready(d_rready),

        .m1_awid(o_awid), .m1_awaddr(o_awaddr), .m1_awlen(o_awlen),
        .m1_awvalid(o_awvalid), .m1_awready(o_awready),
        .m1_wdata(o_wdata), .m1_wstrb(o_wstrb), .m1_wlast(o_wlast),
        .m1_wvalid(o_wvalid), .m1_wready(o_wready),
        .m1_bid(o_bid), .m1_bresp(o_bresp), .m1_bvalid(o_bvalid), .m1_bready(o_bready),
        .m1_arid(o_arid), .m1_araddr(o_araddr), .m1_arlen(o_arlen),
        .m1_arvalid(o_arvalid), .m1_arready(o_arready),
        .m1_rid(o_rid), .m1_rdata(o_rdata), .m1_rresp(o_rresp),
        .m1_rlast(o_rlast), .m1_rvalid(o_rvalid), .m1_rready(o_rready),

        .s0_awid(s0_awid), .s0_awaddr(s0_awaddr), .s0_awlen(s0_awlen),
        .s0_awvalid(s0_awvalid), .s0_awready(s0_awready),
        .s0_wdata(s0_wdata), .s0_wstrb(s0_wstrb), .s0_wlast(s0_wlast),
        .s0_wvalid(s0_wvalid), .s0_wready(s0_wready),
        .s0_bid(s0_bid), .s0_bresp(s0_bresp), .s0_bvalid(s0_bvalid), .s0_bready(s0_bready),
        .s0_arid(s0_arid), .s0_araddr(s0_araddr), .s0_arlen(s0_arlen),
        .s0_arvalid(s0_arvalid), .s0_arready(s0_arready),
        .s0_rid(s0_rid), .s0_rdata(s0_rdata), .s0_rresp(s0_rresp),
        .s0_rlast(s0_rlast), .s0_rvalid(s0_rvalid), .s0_rready(s0_rready),

        .s1_awid(s1_awid), .s1_awaddr(s1_awaddr), .s1_awlen(s1_awlen),
        .s1_awvalid(s1_awvalid), .s1_awready(s1_awready),
        .s1_wdata(s1_wdata), .s1_wstrb(s1_wstrb), .s1_wlast(s1_wlast),
        .s1_wvalid(s1_wvalid), .s1_wready(s1_wready),
        .s1_bid(s1_bid), .s1_bresp(s1_bresp), .s1_bvalid(s1_bvalid), .s1_bready(s1_bready),
        .s1_arid(s1_arid), .s1_araddr(s1_araddr), .s1_arlen(s1_arlen),
        .s1_arvalid(s1_arvalid), .s1_arready(s1_arready),
        .s1_rid(s1_rid), .s1_rdata(s1_rdata), .s1_rresp(s1_rresp),
        .s1_rlast(s1_rlast), .s1_rvalid(s1_rvalid), .s1_rready(s1_rready)
    );

    wire mo_irq;
    main_orch #(.ADDR_W(32), .ID_W(4), .NCMD(128)) u_orch (
        .clk(clk), .resetn(rstn),
        .s_awid(s0_awid), .s_awaddr(s0_awaddr), .s_awlen(s0_awlen),
        .s_awvalid(s0_awvalid), .s_awready(s0_awready),
        .s_wdata(s0_wdata), .s_wstrb(s0_wstrb), .s_wlast(s0_wlast),
        .s_wvalid(s0_wvalid), .s_wready(s0_wready),
        .s_bid(s0_bid), .s_bresp(s0_bresp), .s_bvalid(s0_bvalid), .s_bready(s0_bready),
        .s_arid(s0_arid), .s_araddr(s0_araddr), .s_arlen(s0_arlen),
        .s_arvalid(s0_arvalid), .s_arready(s0_arready),
        .s_rid(s0_rid), .s_rdata(s0_rdata), .s_rresp(s0_rresp),
        .s_rlast(s0_rlast), .s_rvalid(s0_rvalid), .s_rready(s0_rready),
        .m_awid(o_awid), .m_awaddr(o_awaddr), .m_awlen(o_awlen),
        .m_awvalid(o_awvalid), .m_awready(o_awready),
        .m_wdata(o_wdata), .m_wstrb(o_wstrb), .m_wlast(o_wlast),
        .m_wvalid(o_wvalid), .m_wready(o_wready),
        .m_bid(o_bid), .m_bresp(o_bresp), .m_bvalid(o_bvalid), .m_bready(o_bready),
        .m_arid(o_arid), .m_araddr(o_araddr), .m_arlen(o_arlen),
        .m_arvalid(o_arvalid), .m_arready(o_arready),
        .m_rid(o_rid), .m_rdata(o_rdata), .m_rresp(o_rresp),
        .m_rlast(o_rlast), .m_rvalid(o_rvalid), .m_rready(o_rready),
        .irq(mo_irq)
    );

    // ---------------------------------------------------- mesh 2x2 (1..2)
    wire [FW-1:0] l_in [0:1][0:1], l_out [0:1][0:1];
    wire l_inv [0:1][0:1], l_outv [0:1][0:1], l_inb [0:1][0:1], l_outb [0:1][0:1];
    wire [FW-1:0] n_in [0:1][0:1], n_out [0:1][0:1];
    wire n_inv [0:1][0:1], n_outv [0:1][0:1], n_inb [0:1][0:1], n_outb [0:1][0:1];
    wire [FW-1:0] e_in [0:1][0:1], e_out [0:1][0:1];
    wire e_inv [0:1][0:1], e_outv [0:1][0:1], e_inb [0:1][0:1], e_outb [0:1][0:1];
    wire [FW-1:0] w_in [0:1][0:1], w_out [0:1][0:1];
    wire w_inv [0:1][0:1], w_outv [0:1][0:1], w_inb [0:1][0:1], w_outb [0:1][0:1];
    wire [FW-1:0] s_in [0:1][0:1], s_out [0:1][0:1];
    wire s_inv [0:1][0:1], s_outv [0:1][0:1], s_inb [0:1][0:1], s_outb [0:1][0:1];

    genvar gx, gy;
    generate
    for (gx = 0; gx < 2; gx = gx + 1) begin : g_col
      for (gy = 0; gy < 2; gy = gy + 1) begin : g_row
        NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("distributed"),
                    .POS_WIDTH(PW), .POS_X(gx+1), .POS_Y(gy+1),
                    .GRID_LO(1), .GRID_HI(2)) rtr (
            .clk(clk), .rst(!rstn),
            .local_in_data(l_in[gx][gy]), .local_in_valid(l_inv[gx][gy]), .local_in_busy(l_inb[gx][gy]),
            .local_out_data(l_out[gx][gy]), .local_out_valid(l_outv[gx][gy]), .local_out_busy(l_outb[gx][gy]),
            .north_in_data(n_in[gx][gy]), .north_in_valid(n_inv[gx][gy]), .north_in_busy(n_inb[gx][gy]),
            .north_out_data(n_out[gx][gy]), .north_out_valid(n_outv[gx][gy]), .north_out_busy(n_outb[gx][gy]),
            .east_in_data(e_in[gx][gy]), .east_in_valid(e_inv[gx][gy]), .east_in_busy(e_inb[gx][gy]),
            .east_out_data(e_out[gx][gy]), .east_out_valid(e_outv[gx][gy]), .east_out_busy(e_outb[gx][gy]),
            .west_in_data(w_in[gx][gy]), .west_in_valid(w_inv[gx][gy]), .west_in_busy(w_inb[gx][gy]),
            .west_out_data(w_out[gx][gy]), .west_out_valid(w_outv[gx][gy]), .west_out_busy(w_outb[gx][gy]),
            .south_in_data(s_in[gx][gy]), .south_in_valid(s_inv[gx][gy]), .south_in_busy(s_inb[gx][gy]),
            .south_out_data(s_out[gx][gy]), .south_out_valid(s_outv[gx][gy]), .south_out_busy(s_outb[gx][gy])
        );
      end
    end
    for (gy = 0; gy < 2; gy = gy + 1) begin : g_ew
        assign w_in[1][gy] = e_out[0][gy];  assign w_inv[1][gy] = e_outv[0][gy];
        assign e_outb[0][gy] = w_inb[1][gy];
        assign e_in[0][gy] = w_out[1][gy];  assign e_inv[0][gy] = w_outv[1][gy];
        assign w_outb[1][gy] = e_inb[0][gy];
    end
    for (gx = 0; gx < 2; gx = gx + 1) begin : g_ns
        assign n_in[gx][1] = s_out[gx][0];  assign n_inv[gx][1] = s_outv[gx][0];
        assign s_outb[gx][0] = n_inb[gx][1];
        assign s_in[gx][0] = n_out[gx][1];  assign s_inv[gx][0] = n_outv[gx][1];
        assign n_outb[gx][1] = s_inb[gx][0];
    end
    endgenerate

    // dead edges, one by one
    assign e_in[1][0] = {FW{1'b0}}; assign e_inv[1][0] = 1'b0; assign e_outb[1][0] = 1'b0;
    assign e_in[1][1] = {FW{1'b0}}; assign e_inv[1][1] = 1'b0; assign e_outb[1][1] = 1'b0;
    assign n_in[0][0] = {FW{1'b0}}; assign n_inv[0][0] = 1'b0; assign n_outb[0][0] = 1'b0;
    assign n_in[1][0] = {FW{1'b0}}; assign n_inv[1][0] = 1'b0; assign n_outb[1][0] = 1'b0;
    assign s_in[0][1] = {FW{1'b0}}; assign s_inv[0][1] = 1'b0; assign s_outb[0][1] = 1'b0;
    // West of row 2, which the agent used to occupy as a node of its own. It
    // shares MAG's memory ports now, so nothing hangs here.
    assign w_in[0][1] = {FW{1'b0}}; assign w_inv[0][1] = 1'b0; assign w_outb[0][1] = 1'b0;
    assign s_in[1][1] = {FW{1'b0}}; assign s_inv[1][1] = 1'b0; assign s_outb[1][1] = 1'b0;
    // Column 2's locals, which the accumulators used to occupy. A cluster is one
    // node now, so the routers stay and their locals hang free.
    assign l_in[1][0] = {FW{1'b0}}; assign l_inv[1][0] = 1'b0; assign l_outb[1][0] = 1'b0;
    assign l_in[1][1] = {FW{1'b0}}; assign l_inv[1][1] = 1'b0; assign l_outb[1][1] = 1'b0;

    // ---------------------------------------------------------------- MAG
    // memory port on (1,1) west = coordinate (0,1)
    // agent  port on (1,2) west = coordinate (0,2)
    // One AXI channel per MAG memory port, plus one for the host upload.
    localparam integer MEMP = 1;
    localparam integer NCH  = 1;   // ports, upload, mover

    wire [NCH*4-1:0]    r_awid, r_arid, r_bid, r_rid;
    // NCH*40, not NCH*34. Both mag and axi_ram are ADDR_W=40, so at 34 the
    // flattened bus is 18 bits short and the TOP channel loses its upper bits.

    // The mover is that channel: `rptr` truncated the X away and stayed 512
    // while `mem[s_araddr >> LSB]` indexed with it, so beat 0 alone read X.
    wire [NCH*40-1:0]   r_awaddr, r_araddr;
    wire [NCH*8-1:0]    r_awlen, r_arlen;
    wire [NCH*3-1:0]    r_awsize, r_arsize;
    wire [NCH*2-1:0]    r_awburst, r_arburst, r_bresp, r_rresp;
    wire [NCH-1:0]      r_awvalid, r_awready, r_wvalid, r_wready, r_wlast;
    wire [NCH-1:0]      r_bvalid, r_bready, r_arvalid, r_arready;
    wire [NCH-1:0]      r_rvalid, r_rready, r_rlast;
    wire [NCH*DW-1:0]   r_wdata, r_rdata;
    wire [NCH*DW/8-1:0] r_wstrb;
    wire [15:0]  mag_rd, mag_wr;

    // Host upload port. Cluster 0's operands go in by backdoor because that is
    // not what this bench is testing; cluster 1's go through here, with the
    // quantise marker in the top address bits.
    reg  [39:0]   h_awaddr = 0;
    reg  [7:0]    h_awlen  = 0;
    reg           h_awvalid = 0, h_wvalid = 0, h_wlast = 0, h_bready = 0;
    reg  [DW-1:0] h_wdata = 0;
    wire          h_awready, h_wready, h_bvalid;

    // the mover's status, brought out of MAG
    wire         mv_busy;
    wire [3:0]   mv_fault;
    wire [31:0]  mv_done;
    integer      mvspin;

    // Through the CONTROL WINDOW, not a sideband port -- the mover's offsets
    // pass through A_MV_CFG unchanged, so `a` is still its own register offset.
    localparam [31:0] A_MV_CFG = 32'h0800;
    task mvwr(input [7:0] a, input [63:0] d);
        begin drv_write(MAG_BASE + A_MV_CFG + {24'd0, a}, d); end
    endtask

    // `base` is ADDR_W wide and the mover reads it at cfg_data[4 +: ADDR_W], so
    // the 6-bit filler that padded a 34-bit base to the field is gone at 40.
    task mvhdr(input sel, input [39:0] base, input [2:0] nd);
        begin mvwr(8'h10, {17'd0, nd, base, 3'd0, sel}); end
    endtask

    task mvdim(input sel, input [2:0] d, input [15:0] cnt,
               input signed [31:0] strd);
        begin
            mvwr(8'h18, {12'd0, strd, cnt, d, sel});
            mvwr(8'h20, 64'd0);
        end
    endtask

    mag #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(40), .ID_W(4),
          .MEM_PORTS(MEMP),
          .MEM_X(0), .MEM_Y(1),
          .GRID_LO(1), .GRID_HI(2), .STAGE_FLITS(128)) u_mag (
        .clk(clk), .resetn(rstn),
        .sm_awid(4'd0), .sm_awaddr(h_awaddr), .sm_awlen(h_awlen),
        .sm_awvalid(h_awvalid), .sm_awready(h_awready),
        .sm_wdata(h_wdata), .sm_wstrb({(DW/8){1'b1}}),
        .sm_wlast(h_wlast), .sm_wvalid(h_wvalid), .sm_wready(h_wready),
        .sm_bid(), .sm_bresp(), .sm_bvalid(h_bvalid), .sm_bready(h_bready),
        .sm_arid(4'd0), .sm_araddr(34'd0), .sm_arlen(8'd0), .sm_arvalid(1'b0),
        .sm_arready(), .sm_rid(), .sm_rdata(), .sm_rresp(), .sm_rlast(),
        .sm_rvalid(), .sm_rready(1'b1),
        // control slave: from the crossbar
        .sc_awid(s1_awid), .sc_awaddr(s1_awaddr), .sc_awlen(s1_awlen),
        .sc_awvalid(s1_awvalid), .sc_awready(s1_awready),
        .sc_wdata(s1_wdata), .sc_wstrb(s1_wstrb), .sc_wlast(s1_wlast),
        .sc_wvalid(s1_wvalid), .sc_wready(s1_wready),
        .sc_bid(s1_bid), .sc_bresp(s1_bresp), .sc_bvalid(s1_bvalid), .sc_bready(s1_bready),
        .sc_arid(s1_arid), .sc_araddr(s1_araddr), .sc_arlen(s1_arlen),
        .sc_arvalid(s1_arvalid), .sc_arready(s1_arready),
        .sc_rid(s1_rid), .sc_rdata(s1_rdata), .sc_rresp(s1_rresp),
        .sc_rlast(s1_rlast), .sc_rvalid(s1_rvalid), .sc_rready(s1_rready),
        // master to RAM
        .dram_aclk(clk), .dram_aresetn(rstn),
        .dram_awid(r_awid), .dram_awaddr(r_awaddr), .dram_awlen(r_awlen),
        .dram_awsize(r_awsize), .dram_awburst(r_awburst),
        .dram_awvalid(r_awvalid), .dram_awready(r_awready),
        .dram_wdata(r_wdata), .dram_wstrb(r_wstrb), .dram_wlast(r_wlast),
        .dram_wvalid(r_wvalid), .dram_wready(r_wready),
        .dram_bid(r_bid), .dram_bresp(r_bresp), .dram_bvalid(r_bvalid), .dram_bready(r_bready),
        .dram_arid(r_arid), .dram_araddr(r_araddr), .dram_arlen(r_arlen),
        .dram_arsize(r_arsize), .dram_arburst(r_arburst),
        .dram_arvalid(r_arvalid), .dram_arready(r_arready),
        .dram_rid(r_rid), .dram_rdata(r_rdata), .dram_rresp(r_rresp),
        .dram_rlast(r_rlast), .dram_rvalid(r_rvalid), .dram_rready(r_rready),
        // NoC
        .mem_in_data(w_out[0][0]), .mem_in_valid(w_outv[0][0]), .mem_in_busy(w_outb[0][0]),
        .mem_out_data(w_in[0][0]), .mem_out_valid(w_inv[0][0]), .mem_out_busy(w_inb[0][0]),
        // The agent used to take the west of row 2 as a node of its own. It
        // shares the memory ports now and answers at (MEM_X, MEM_Y), so that
        // edge is tied off below.
        .mem_rd_count(mag_rd), .mem_wr_count(mag_wr),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done)
    );

    reg          bd_we = 0;
    reg  [15:0]  bd_addr = 0;
    reg  [DW-1:0] bd_wdata = 0;
    wire [DW-1:0] bd_rdata;

    axi_ram #(.DATA_W(DW), .ADDR_W(40), .ID_W(4), .WORDS(1024),
              .PORTS(NCH)) u_ram (
        .clk(clk), .resetn(rstn),
        .s_awid(r_awid), .s_awaddr(r_awaddr), .s_awlen(r_awlen),
        .s_awsize(r_awsize), .s_awburst(r_awburst),
        .s_awvalid(r_awvalid), .s_awready(r_awready),
        .s_wdata(r_wdata), .s_wstrb(r_wstrb), .s_wlast(r_wlast),
        .s_wvalid(r_wvalid), .s_wready(r_wready),
        .s_bid(r_bid), .s_bresp(r_bresp), .s_bvalid(r_bvalid), .s_bready(r_bready),
        .s_arid(r_arid), .s_araddr(r_araddr), .s_arlen(r_arlen),
        .s_arsize(r_arsize), .s_arburst(r_arburst),
        .s_arvalid(r_arvalid), .s_arready(r_arready),
        .s_rid(r_rid), .s_rdata(r_rdata), .s_rresp(r_rresp),
        .s_rlast(r_rlast), .s_rvalid(r_rvalid), .s_rready(r_rready),
        .bd_we(bd_we), .bd_addr(bd_addr), .bd_wdata(bd_wdata), .bd_rdata(bd_rdata)
    );

    // ------------------------------------------------------------ clusters
    wire [15:0] f0, g0, d0, f1, g1, d1;

    mx_cluster_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                    .CU_X(1), .CU_Y(1),
                    .MEM_X(0), .MEM_Y(1),
                    .TILES(16), .GA(16), .GB(16), .MODEL(MODEL)) cu0 (
        .clk(clk), .resetn(rstn),
        .noc_in_data(l_out[0][0]), .noc_in_valid(l_outv[0][0]),
        .noc_in_busy(l_outb[0][0]),
        .noc_out_data(l_in[0][0]), .noc_out_valid(l_inv[0][0]),
        .noc_out_busy(l_inb[0][0]),
        .fills_done(f0), .gemms_done(g0), .drains_done(d0)
    );

    mx_cluster_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                    .CU_X(1), .CU_Y(2),
                    .MEM_X(0), .MEM_Y(1),
                    .TILES(16), .GA(16), .GB(16), .MODEL(MODEL)) cu1 (
        .clk(clk), .resetn(rstn),
        .noc_in_data(l_out[0][1]), .noc_in_valid(l_outv[0][1]),
        .noc_in_busy(l_outb[0][1]),
        .noc_out_data(l_in[0][1]), .noc_out_valid(l_inv[0][1]),
        .noc_out_busy(l_inb[0][1]),
        .fills_done(f1), .gemms_done(g1), .drains_done(d1)
    );

    // ======================================================= driver AXI tasks
    task drv_write(input [31:0] a, input [63:0] d);
        begin
            @(posedge clk);
            d_awaddr <= a; d_awlen <= 0; d_awid <= 4'h1; d_awvalid <= 1'b1;
            @(posedge clk); while (!d_awready) @(posedge clk);
            d_awvalid <= 1'b0;
            d_wdata <= d; d_wlast <= 1'b1; d_wvalid <= 1'b1; d_bready <= 1'b1;
            @(posedge clk); while (!d_wready) @(posedge clk);
            d_wvalid <= 1'b0; d_wlast <= 1'b0;
            @(posedge clk); while (!d_bvalid) @(posedge clk);
            d_bready <= 1'b0;
        end
    endtask

    task drv_read(input [31:0] a, output [63:0] d);
        begin
            @(posedge clk);
            d_araddr <= a; d_arlen <= 0; d_arid <= 4'h2; d_arvalid <= 1'b1;
            @(posedge clk); while (!d_arready) @(posedge clk);
            d_arvalid <= 1'b0; d_rready <= 1'b1;
            @(posedge clk); while (!d_rvalid) @(posedge clk);
            d = d_rdata;
            @(posedge clk); d_rready <= 1'b0;
        end
    endtask

    // ---- the driver-side instruction set -------------------------------
    integer ncmd;
    task cmd_wr(input [31:0] a, input [63:0] v);
        begin
            drv_write(ORC_BASE + MO_CMD + ncmd*32 + 0, {60'd0, OP_WR});
            drv_write(ORC_BASE + MO_CMD + ncmd*32 + 8, {32'd0, a});
            drv_write(ORC_BASE + MO_CMD + ncmd*32 + 16, v);
            ncmd = ncmd + 1;
        end
    endtask
    task cmd_poll(input [31:0] a, input [63:0] want, input [63:0] mask);
        begin
            drv_write(ORC_BASE + MO_CMD + ncmd*32 + 0, {60'd0, OP_POLL});
            drv_write(ORC_BASE + MO_CMD + ncmd*32 + 8, {32'd0, a});
            drv_write(ORC_BASE + MO_CMD + ncmd*32 + 16, want);
            drv_write(ORC_BASE + MO_CMD + ncmd*32 + 24, mask);
            ncmd = ncmd + 1;
        end
    endtask
    task cmd_done(input [63:0] c);
        begin
            drv_write(ORC_BASE + MO_CMD + ncmd*32 + 0, {60'd0, OP_DONE});
            drv_write(ORC_BASE + MO_CMD + ncmd*32 + 16, c);
            ncmd = ncmd + 1;
        end
    endtask

    // ============================================================= problem
    integer signed A [0:M-1][0:KK-1];
    integer signed B [0:KK-1][0:N-1];
    integer SA [0:M-1], SB [0:N-1], ANCHOR;
    real  fp64_c [0:M-1][0:N-1];
    real  hw_c   [0:M-1][0:N-1];
    integer errors = 0, checks = 0;
    real worst_rel = 0.0, sum_rel = 0.0;
    integer nz = 0;

    function real fp16_to_real(input [15:0] f);
        real m; integer e;
        begin
            if (f[14:10] == 5'd0) fp16_to_real = 0.0;
            else begin
                m = 1.0 + $itor(f[9:0]) / 1024.0;
                e = f[14:10] - 15;
                fp16_to_real = m * (2.0 ** e);
                if (f[15]) fp16_to_real = -fp16_to_real;
            end
        end
    endfunction

    // Only ever called on v * 2^s for |v| <= 63, which is exactly representable
    // in FP16 -- so this needs no rounding and deliberately has none. A value
    // that would round is a bug in the caller, not something to absorb here.
    function [15:0] real_to_fp16(input real x);
        real a; integer e, eb, m; reg sgn;
        begin
            sgn = (x < 0.0);
            a   = sgn ? -x : x;
            if (a == 0.0) real_to_fp16 = 16'd0;
            else begin
                e = 0;
                while (a >= 2.0) begin a = a / 2.0; e = e + 1; end
                while (a <  1.0) begin a = a * 2.0; e = e - 1; end
                m  = $rtoi((a - 1.0) * 1024.0 + 0.5);
                eb = e + 15;
                real_to_fp16 = {sgn, eb[4:0], m[9:0]};
            end
        end
    endfunction

    task bd_write(input integer w, input [DW-1:0] d);
        begin
            @(negedge clk);
            bd_we <= 1'b1; bd_addr <= w[15:0]; bd_wdata <= d;
            @(negedge clk); bd_we <= 1'b0;
        end
    endtask

    // ---- the upload path -------------------------------------------------
    // One burst of FP16 into MAG's memory window. Bit 33 asks for quantise as
    // it lands, bit 32 selects B packing; the rest is the DESTINATION, in the
    // int7 layout. Every handshake is spun on the edge that carries it.
    reg [DW-1:0] hsrc [0:127];
    integer      hb, hn;

    task host_push(input [39:0] dst, input quant, input blay, input integer nb);
        begin
            hn        = nb;
            // {special, rsvd, mesh, aperture 0b010, blay} then 4 GB of target.
            h_awaddr  <= quant ? {4'b1000, 3'b010, blay, dst[31:0]}
                               : {8'd0, dst[31:0]};
            h_awlen   <= hn[7:0] - 8'd1;
            h_awvalid <= 1'b1;
            h_bready  <= 1'b1;
            @(posedge clk); while (!h_awready) @(posedge clk);
            h_awvalid <= 1'b0;
            for (hb = 0; hb < hn; hb = hb + 1) begin
                h_wdata  <= hsrc[hb];
                h_wlast  <= (hb == hn - 1);
                h_wvalid <= 1'b1;
                @(posedge clk); while (!h_wready) @(posedge clk);
            end
            h_wvalid <= 1'b0; h_wlast <= 1'b0;
            @(posedge clk); while (!h_bvalid) @(posedge clk);
            h_bready <= 1'b0;
        end
    endtask

    // ---- one CU instruction flit ----------------------------------------
    // Written out field by field so it cannot drift from the decode. It did:
    // `n` widened to 16 bits when the resident tile reached 512 sub-tiles, and
    // a concatenation that still spelled it as 8 shifts every field below it.
    // See docs/isa/cluster.md s2.
    function [FW-1:0] cu_inst;
        input [3:0]  op;
        input [33:0] adr;
        input [15:0] nn;
        input        sel;
        input        acc;
        input [7:0]  i_gm, i_gn, i_nk, i_anch;
        input        preq;
        input        emit;      // GEMM: hand sub-tiles out as they finish
        input        fuse;      // DRAIN: they already came out; just wait
        input        lst;
        begin
            //  ... peers(24) npeer(2) preq eoff(8) aoff(8) boff(8) emit fuse
            cu_inst = { 16'h0000, 4'h5, 8'h40, lst, 3'b000,
                        op, adr, nn, sel, acc,
                        i_gm, i_gn, i_nk, i_anch,
                        24'd0, 2'd0, preq, 24'd0, emit, fuse, 115'd0 };
        end
    endfunction

    integer i, j, k, g, h, c, t, seed, cu, kb;
    integer signed bsum;
    reg [DW-1:0] wtmp, res_word;
    reg [63:0]  rv;
    reg [33:0]  ad34;
    reg         preq, fuse;
    real got_r, want_r, err;

    // stage one CU instruction flit at program slot `slot` for the agent
    task stage_flit(input integer slot, input [FW-1:0] fl);
        begin
            for (t = 0; t < 5; t = t + 1)
                cmd_wr(MAG_BASE + A_STAGE + (slot*5 + t)*8, fl[t*64 +: 64]);
        end
    endtask

    initial begin
        // 2*SBIAS cancels the bias stored in each E5M3 scale field; HEADROOM
        // keeps the drained tile below true scale so it stays inside FP16.
        seed = 32'h0BADF00D; ANCHOR = 2*SBIAS + HEADROOM; ncmd = 0;
        #200; repeat (10) @(posedge clk); rstn = 1; repeat (10) @(posedge clk);

        $display("--- 1. problem: C[%0d,%0d] = A[%0d,%0d] x B[%0d,%0d] ---", M, N, M, KK, KK, N);
        for (i = 0; i < M; i = i + 1)
            for (k = 0; k < KK; k = k + 1) A[i][k] = ($random(seed) & 63) - 32;
        for (k = 0; k < KK; k = k + 1)
            for (j = 0; j < N; j = j + 1) B[k][j] = ($random(seed) & 63) - 32;
        for (i = 0; i < M; i = i + 1) SA[i] = ($random(seed) & 3);
        for (j = 0; j < N; j = j + 1) SB[j] = ($random(seed) & 3);

        // Pin each lane's peak into [60,63]. MAG derives the block scale from
        // the peak, and for the integer model below to hold the quantiser must
        // recover v and s exactly -- which needs the scale to land on 2^s.
        // E5M3 rounds peak/63 up to the nearest eighth, so that requires the
        // peak to be 60 or more; E8M0 accepted anything in [32,64).
        // ONE PEAK PER BLOCK, not per lane: the scale is shared along K within
        // a 32-element block, so every block of every lane needs its own.
        for (k = 0; k < NKB; k = k + 1) begin
            for (i = 0; i < M; i = i + 1) A[i][k*32] = 60 + ($random(seed) & 3);
            for (j = 0; j < N; j = j + 1) B[k*32][j] = 60 + ($random(seed) & 3);
        end

        for (i = 0; i < M; i = i + 1)
            for (j = 0; j < N; j = j + 1) begin
                bsum = 0;
                for (k = 0; k < KK; k = k + 1) bsum = bsum + A[i][k]*B[k][j];
                fp64_c[i][j] = $itor(bsum) * (2.0 ** (SA[i]+SB[j]-HEADROOM));
            end

        // Software never stores MXFP7, so what goes into memory is FP16: one
        // L1 entry is 4 lanes x 32 FP16 = 8 words. The values are v * 2^s, the
        // exact fixed point of the quantiser, so the integer model above stays
        // valid with the real circuit in path.
        //
        // Cluster 0's copy goes in by backdoor. Cluster 1's goes in through
        // MAG's memory window with the quantise marker set, so it lands as
        // int7 and its FILLs read half as many bytes with no quantiser pass.
        $display("--- 2. operands: backdoor FP16, and an upload that quantises ---");
        // Entry (group, K block) is at `group*NKB + block` -- group-major,
        // block-minor, which is the order the sweep reads them in.
        for (g = 0; g < GM; g = g + 1)
            for (kb = 0; kb < NKB; kb = kb + 1)
                for (c = 0; c < WPE; c = c + 1) begin
                    wtmp = {DW{1'b0}};
                    for (t = 0; t < 16; t = t + 1) begin
                        i = (c*16 + t) / 32;          // lane within the entry
                        k = kb*32 + ((c*16 + t) % 32);
                        wtmp[t*16 +: 16] =
                            real_to_fp16($itor(A[g*4+i][k]) * (2.0 ** SA[g*4+i]));
                    end
                    bd_write(WA_BASE + (g*NKB + kb)*WPE + c, wtmp);
                    hsrc[(g*NKB + kb)*WPE + c] = wtmp;
                end
        host_push(WQA_BASE * 32, 1'b1, 1'b0, GM*NKB*WPE);

        for (h = 0; h < GNC*2; h = h + 1)
            for (kb = 0; kb < NKB; kb = kb + 1)
                for (c = 0; c < WPE; c = c + 1) begin
                    wtmp = {DW{1'b0}};
                    for (t = 0; t < 16; t = t + 1) begin
                        j = (c*16 + t) / 32;          // lane = column of B
                        k = kb*32 + ((c*16 + t) % 32);
                        wtmp[t*16 +: 16] =
                            real_to_fp16($itor(B[k][h*4+j]) * (2.0 ** SB[h*4+j]));
                    end
                    if (h < GNC) bd_write(WB0_BASE + (h*NKB + kb)*WPE + c, wtmp);
                    else begin
                        bd_write(WB1_BASE + ((h-GNC)*NKB + kb)*WPE + c, wtmp);
                        hsrc[((h-GNC)*NKB + kb)*WPE + c] = wtmp;
                    end
                end
        host_push(WQB_BASE * 32, 1'b1, 1'b1, GNC*NKB*WPE);

        // ---------------------------------------------------------------
        $display("--- 3. build the control program (driver ISA) ---");
        for (cu = 0; cu < 2; cu = cu + 1) begin
            // four CU instructions: FILL A, FILL B, GEMM, DRAIN.
            // Cluster 1 reads the pre-quantised copy of the same numbers.
            // Cluster 1 takes the pre-quantised operands AND the fused drain;
            // cluster 0 stays on the online-quantised, explicitly-drained
            // path. Both must produce the same exact integers.
            preq = (cu == 1);
            fuse = (cu == 1);
            ad34 = (preq ? WQA_BASE : WA_BASE) * 32;
            stage_flit(0, cu_inst(4'd1, ad34, GM[15:0]*NKB[15:0], 1'b0, 1'b0,
                                  8'd0, 8'd0, 8'd0, 8'd0,
                                  preq, 1'b0, 1'b0, 1'b0));

            ad34 = (preq ? WQB_BASE : ((cu == 0) ? WB0_BASE : WB1_BASE)) * 32;
            stage_flit(1, cu_inst(4'd1, ad34, GNC[15:0]*NKB[15:0], 1'b1, 1'b0,
                                  8'd0, 8'd0, 8'd0, 8'd0,
                                  preq, 1'b0, 1'b0, 1'b0));

            ad34 = ((cu == 0) ? WC0_BASE : WC1_BASE) * 32;
            stage_flit(2, cu_inst(4'd2, fuse ? ad34 : 34'd0, 16'd0, 1'b0, 1'b0,
                                  GM[7:0], GNC[7:0], NKB[7:0], ANCHOR[7:0],
                                  1'b0, fuse, 1'b0, 1'b0));

            stage_flit(3, cu_inst(4'd3, ad34, NT[15:0], 1'b0, 1'b0,
                                  8'd0, 8'd0, 8'd0, ANCHOR[7:0],
                                  1'b0, 1'b0, fuse, 1'b1));

            // dispatch to this cluster's manager, then wait for 4 signals
            cmd_wr(MAG_BASE + A_PROG_DST,  {56'd0, 4'd1 + cu[3:0], 4'd1});
            cmd_wr(MAG_BASE + A_PROG_LEN,  64'd4);
            cmd_wr(MAG_BASE + A_PROG_CRED, 64'd8);
            cmd_wr(MAG_BASE + A_PROG_KICK, 64'd1);
            cmd_poll(MAG_BASE + A_NODE + ({4'd1 + cu[3:0], 4'd1}) * 8,
                     64'h0000_0000_0000_0401,     // signals == 4, valid == 1
                     64'h0000_0000_00FF_FF01);
        end
        cmd_done(64'hC0DE);
        $display("    %0d driver commands", ncmd);

        // ---------------------------------------------------------------
        $display("--- 4. GO: the orchestrator runs it, host just waits ---");
        drv_write(ORC_BASE + MO_CTRL, 64'd1);

        // Bounded in POLLS, and the bound is not generous: this problem is a
        // few thousand cycles and each poll is a full AXI round trip, so a
        // limit large enough to "be safe" is a limit that turns a wedge into
        // minutes of silence. `WATCHDOG` below is the backstop.
        t = 0;
        rv = 64'd0;
        while (!rv[1] && t < 20000) begin
            drv_read(ORC_BASE + MO_CTRL, rv);
            t = t + 1;
        end
        drv_read(ORC_BASE + MO_PC, rv);
        $display("    orchestrator stopped at pc=%0d", rv[15:0]);
        drv_read(ORC_BASE + MO_CODE, rv);
        $display("    DONE code = %04h", rv[15:0]);
        checks = checks + 1;
        if (rv[15:0] !== 16'hC0DE) begin
            errors = errors + 1;
            $display("  FAIL orchestrator did not reach DONE");
        end
        $display("    cu0 f=%0d g=%0d d=%0d   cu1 f=%0d g=%0d d=%0d   mem rd=%0d wr=%0d",
                 f0, g0, d0, f1, g1, d1, mag_rd, mag_wr);

        repeat (400) @(posedge clk);

        // ---------------------------------------------------------------
        $display("--- 5. read results back and check ---");
        for (cu = 0; cu < 2; cu = cu + 1)
            for (t = 0; t < NT; t = t + 1) begin
                @(negedge clk);
                bd_addr <= ((cu == 0) ? WC0_BASE : WC1_BASE) + t;
                @(negedge clk); @(negedge clk);
                res_word = bd_rdata;
                for (i = 0; i < 4; i = i + 1)
                    for (j = 0; j < 4; j = j + 1)
                        hw_c[(t / GNC)*4 + i][cu*(N/2) + (t % GNC)*4 + j]
                            = fp16_to_real(res_word[(i*4+j)*16 +: 16]);
            end

        for (i = 0; i < M; i = i + 1)
            for (j = 0; j < N; j = j + 1) begin
                got_r = hw_c[i][j]; want_r = fp64_c[i][j];
                checks = checks + 1;
                if (want_r == 0.0) begin
                    if (got_r != 0.0) errors = errors + 1;
                end else begin
                    err = (got_r - want_r) / want_r;
                    if (err < 0.0) err = -err;
                    nz = nz + 1; sum_rel = sum_rel + err;
                    if (err > worst_rel) worst_rel = err;
                    if (err > 4.0/1024.0) begin
                        errors = errors + 1;
                        if (errors <= 8)
                            $display("  FAIL C[%0d][%0d] got %0f want %0f rel %0e",
                                     i, j, got_r, want_r, err);
                    end
                end
            end

        // ---- the mover, on MAG's own AXI master, after the GEMM is graded ---
        // A word transpose, which is the tile-order to entry-order shape. It
        // shares only the address space with the ports, so a pass here says
        // the new master reaches memory without disturbing them.
        $display("--- the memory mover through MAG ---");
        for (i = 0; i < 32; i = i + 1) begin
            @(negedge clk);
            bd_we = 1'b1; bd_addr = 16'd512 + i[15:0];
            bd_wdata = {8{32'h5A00_0000 | i[31:0]}};
        end
        @(negedge clk); bd_we = 1'b0;

        mvhdr(1'b0, 34'h4000, 3'd2);
        mvdim(1'b0, 3'd0, 16'd4, 32'sd256);
        mvdim(1'b0, 3'd1, 16'd8, 32'sd32);
        mvhdr(1'b1, 34'h4400, 3'd2);
        mvdim(1'b1, 3'd0, 16'd4, 32'sd32);
        mvdim(1'b1, 3'd1, 16'd8, 32'sd128);
        mvwr(8'h00, {47'd0, 1'b1, 8'd0, 3'd0, 2'd1, 3'd0});

        // WAIT FOR THE RISE, THEN THE FALL. Watching only for `busy` to fall
        // reads as "already finished" when it has not yet started, and the
        // command path is now several cycles of AXI rather than one wire.
        @(negedge clk);
        mvspin = 0;
        while (!mv_busy && mvspin < 1000) begin
            mvspin = mvspin + 1;
            @(negedge clk);
        end
        mvspin = 0;
        while (mv_busy && mvspin < 50000) begin
            mvspin = mvspin + 1;
            @(negedge clk);
        end
        checks = checks + 1;
        if (mvspin >= 50000) begin
            errors = errors + 1;
            $display("  FAIL mover never went idle");
        end
        checks = checks + 1;
        if (mv_fault !== 4'd0) begin
            errors = errors + 1;
            $display("  FAIL mover faulted, code %0d", mv_fault);
        end

        for (i = 0; i < 4; i = i + 1)
            for (j = 0; j < 8; j = j + 1) begin
                @(negedge clk); bd_addr = 16'd544 + j*4 + i;
                @(negedge clk); @(negedge clk);
                checks = checks + 1;
                if (bd_rdata !== {8{32'h5A00_0000 | (i*8+j)}}) begin
                    errors = errors + 1;
                    if (errors <= 12)
                        $display("  FAIL moved word [%0d][%0d] got %h want %h",
                                 i, j, bd_rdata[63:0],
                                 {2{32'h5A00_0000 | (i*8+j)}});
                end
            end

        $display("");
        $display("    C[0][0] hw %0f  fp64 %0f", hw_c[0][0], fp64_c[0][0]);
        $display("    C[0][8] hw %0f  fp64 %0f", hw_c[0][8], fp64_c[0][8]);
        $display("    worst rel err %0e  mean %0e  (FP16 ULP %0e)",
                 worst_rel, sum_rel/$itor(nz), 1.0/1024.0);
        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors  (MODEL=%0d)", checks, MODEL);
        else             $display("  FAIL -- %0d checks, %0d errors  (MODEL=%0d)", checks, errors, MODEL);
        $display("========================================");
        $finish;
    end

    initial begin
        #4000000;                       // 1 M cycles at 4 ns; the run is ~5 k
        $display("WATCHDOG -- f0=%0d d0=%0d f1=%0d d1=%0d rd=%0d wr=%0d",
                 f0, d0, f1, d1, mag_rd, mag_wr);
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire
