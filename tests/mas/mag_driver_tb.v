// Driver-in-the-loop: the control program comes from the host driver.
//
// Nothing about the computation is described here: the Python driver writes the
// files below and this bench loads them, replays them over AXI, and dumps memory
// back. A wrong program from the driver produces a wrong answer here, which is
// the coupling that makes this a test OF the driver.
//
//   operands.hex   the HOST's 256-bit FP16 image, in upload order
//   uploads.hex    {src, dst, words, flags}: the regions the host pushes
//   setup.hex      {addr[63:0], data[63:0]}: host writes made before each GO
//   program.hex    one command per line: {mask, data, addr, op} packed to 256b
//   rounds.hex     {nsetup, ncmd} per round -- where to cut the two above
//   result.hex     device memory image after the run
//
// Software never sees MXFP7: the host uploads FP16 through MAG's memory window
// and the marker on the write address selects whether MAG stores it verbatim or
// quantises it as it lands. NO backdoor preload -- the upload is the path XDMA
// will use, tested rather than assumed.

`default_nettype none
`timescale 1ns/1ps

`ifndef MX_MODEL
`define MX_MODEL 1
`endif

module mag_driver_tb;

    localparam integer FW    = 288;
    localparam integer PW    = 4;
    localparam integer MODEL = `MX_MODEL;
    localparam integer DW    = 256;
    localparam integer NCMD      = 128;      // command RAM, one ROUND's worth
    localparam integer MAX_SETUP = 1 << 18;  // all rounds' host writes
    localparam integer MAX_CMDS  = 8192;     // all rounds' commands
    localparam integer MAX_ROUNDS = 512;
    localparam integer MAX_HOST    = 1 << 18; // host FP16 image, in words
    localparam integer MAX_UPLOADS = 64;      // contiguous upload regions
    // Source beats per upload burst. A multiple of 8 because a quantising
    // upload converts whole entries, and small enough that one burst does not
    // hold MAG's write path for long -- a real host uploads while the machine
    // is still working on the previous layer.
    localparam integer UP_BEATS  = 64;
    // 8 MB, and it must equal `bench.WORDS` -- the driver lays regions out
    // against its own copy of this number, so a disagreement silently
    // overlaps A, B and C rather than failing.
    localparam integer RAM_WORDS = 262144;
    // CLUSTERS, one per COLUMN of a band; a band is two rows. `bench.CLUSTERS`
    // in the driver must list exactly the manager coordinates the mesh below
    // produces, or the dispatcher kicks a node that is not there and the round
    // never retires.
`ifndef NCL
`define NCL 2
`endif
    localparam integer NCL = `NCL;

    // Backstop for the one case the progress watchdog cannot see: a stall in the
    // HOST phase, before GO, where no counter would move anyway. The host AXI
    // handshakes are individually bounded (see `AWAIT), so this only has to
    // outlast the largest legitimate run -- a MEASURED bound, not an estimate:
    // 512x512x512 ran 2,089,993 cycles end to end.
`ifndef WDOG
`define WDOG 4000000
`endif
    localparam integer WDOG_CYCLES = `WDOG;

    localparam [31:0] ORC_BASE = 32'h0000_0000;
    localparam [31:0] MO_CTRL = 32'h0000, MO_PC = 32'h0008, MO_CODE = 32'h0010;
    localparam [31:0] MO_CMD  = 32'h1000;

    // Bit 28 selects MAG over the main orchestrator; see kohakutpu.device.
    localparam [31:0] MAG_BASE    = 32'h1000_0000;
    localparam [31:0] A_PROG_STAT = 32'h0058, A_SIG_DONE = 32'h0070;

    reg clk = 0, rstn = 0;
    always #2 clk = ~clk;

    // Free-running cycle count. Declared here because the instrumentation
    // below is written in terms of it.
    reg [63:0] cycles = 0, cyc0 = 0;
    always @(posedge clk) cycles <= rstn ? cycles + 64'd1 : 64'd0;

    // EVERY profile counter is gated on this, which goes high only once the
    // operands are in memory. The upload is real traffic through the same AXI
    // master -- as many beats as the whole of C -- so a free-running counter
    // absorbs it and reports the drain writing C twice. Measured separately as
    // `UPLOAD`, being paid once per tensor rather than once per pass.
    reg measure = 1'b0;

    // -------------------------------------------------- driver-side AXI
    reg  [3:0]  d_awid=0, d_arid=0;
    reg  [31:0] d_awaddr=0, d_araddr=0;
    reg  [7:0]  d_awlen=0, d_arlen=0;
    reg         d_awvalid=0, d_arvalid=0, d_wvalid=0, d_wlast=0, d_bready=0, d_rready=0;
    reg  [63:0] d_wdata=0;
    wire        d_awready, d_wready, d_bvalid, d_arready, d_rvalid, d_rlast;
    wire [63:0] d_rdata;
    wire [3:0]  d_bid, d_rid;
    wire [1:0]  d_bresp, d_rresp;

    wire [3:0]  o_awid, o_arid, o_bid, o_rid;
    wire [31:0] o_awaddr, o_araddr;
    wire [7:0]  o_awlen, o_arlen, o_wstrb;
    wire        o_awvalid, o_awready, o_wvalid, o_wready, o_wlast;
    wire        o_bvalid, o_bready, o_arvalid, o_arready, o_rvalid, o_rready, o_rlast;
    wire [63:0] o_wdata, o_rdata;
    wire [1:0]  o_bresp, o_rresp;

    wire [3:0]  s0_awid, s0_arid, s0_bid, s0_rid, s1_awid, s1_arid, s1_bid, s1_rid;
    wire [31:0] s0_awaddr, s0_araddr, s1_awaddr, s1_araddr;
    wire [7:0]  s0_awlen, s0_arlen, s0_wstrb, s1_awlen, s1_arlen, s1_wstrb;
    wire        s0_awvalid, s0_awready, s0_wvalid, s0_wready, s0_wlast;
    wire        s0_bvalid, s0_bready, s0_arvalid, s0_arready, s0_rvalid, s0_rready, s0_rlast;
    wire        s1_awvalid, s1_awready, s1_wvalid, s1_wready, s1_wlast;
    wire        s1_bvalid, s1_bready, s1_arvalid, s1_arready, s1_rvalid, s1_rready, s1_rlast;
    wire [63:0] s0_wdata, s0_rdata, s1_wdata, s1_rdata;
    wire [1:0]  s0_bresp, s0_rresp, s1_bresp, s1_rresp;

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
    main_orch #(.ADDR_W(32), .ID_W(4), .NCMD(NCMD)) u_orch (
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

    // ------------------------------------------- mesh: NCOL columns x NROW rows
    // ONE CLUSTER PER ROUTER LOCAL, filling row-major, so the grid is exactly as
    // large as the cluster count needs: NCL=2 is 1x2, 4 is 1x4, 8 is 2x4.
    //
    // ROWS BEFORE COLUMNS, because a row is a MAG memory port and a column is
    // not. Four is the ceiling -- mag.v declares four port coordinate pairs -- so
    // 8 is 2x4 rather than 4x2, which would leave half the ports unbuilt on a
    // machine whose drain is already its limit.
    //
    // Replaces a two-row band whose inner locals hung free. That kept the mesh
    // comparable across the accumulator-port merge and has outlived it: the real
    // machine is maps/mesh_2x4.txt, and a bench with twice the routers measures a
    // fabric less contended than the one that gets built.
    //
    // MAG hangs off the WEST edge, one port per row at (0,1)..(0,NROW), each
    // carrying operand AND agent traffic demuxed by flit type, so north, south
    // and east are free. Routing is X-then-Y on CLAMPED coordinates: a flit for
    // (0,1) routes toward router (1,1) and takes the outward hop on arrival.
    // The last row need not be full. See docs/noc/spec.md s2.
    localparam integer NROW0 = (NCL >= 4) ? 4 : NCL;      // one MAG port each
    localparam integer NCOL  = (NCL + NROW0 - 1) / NROW0;
    // Tighten: 9 clusters over 4 rows needs 3 columns, and 3 columns need only
    // 3 rows. Without this a fourth row is built whose MAG port serves nothing.
    localparam integer NROW  = (NCL + NCOL - 1) / NCOL;
    localparam integer GHI   = (NROW > NCOL) ? NROW : NCOL;

    wire [FW-1:0] l_in [0:NCOL-1][0:NROW-1], l_out [0:NCOL-1][0:NROW-1];
    wire l_inv [0:NCOL-1][0:NROW-1], l_outv [0:NCOL-1][0:NROW-1];
    wire l_inb [0:NCOL-1][0:NROW-1], l_outb [0:NCOL-1][0:NROW-1];
    wire [FW-1:0] n_in [0:NCOL-1][0:NROW-1], n_out [0:NCOL-1][0:NROW-1];
    wire n_inv [0:NCOL-1][0:NROW-1], n_outv [0:NCOL-1][0:NROW-1];
    wire n_inb [0:NCOL-1][0:NROW-1], n_outb [0:NCOL-1][0:NROW-1];
    wire [FW-1:0] e_in [0:NCOL-1][0:NROW-1], e_out [0:NCOL-1][0:NROW-1];
    wire e_inv [0:NCOL-1][0:NROW-1], e_outv [0:NCOL-1][0:NROW-1];
    wire e_inb [0:NCOL-1][0:NROW-1], e_outb [0:NCOL-1][0:NROW-1];
    wire [FW-1:0] w_in [0:NCOL-1][0:NROW-1], w_out [0:NCOL-1][0:NROW-1];
    wire w_inv [0:NCOL-1][0:NROW-1], w_outv [0:NCOL-1][0:NROW-1];
    wire w_inb [0:NCOL-1][0:NROW-1], w_outb [0:NCOL-1][0:NROW-1];
    wire [FW-1:0] s_in [0:NCOL-1][0:NROW-1], s_out [0:NCOL-1][0:NROW-1];
    wire s_inv [0:NCOL-1][0:NROW-1], s_outv [0:NCOL-1][0:NROW-1];
    wire s_inb [0:NCOL-1][0:NROW-1], s_outb [0:NCOL-1][0:NROW-1];

    // ONE MAG PORT PER MESH ROW. A port attaches to the NoC, not to a cluster,
    // so every cluster can reach every port and the row only decides which edge
    // its traffic leaves by. MAG's AXI masters are one per port plus one for the
    // host upload -- sharing them would put eight clusters' read beats back on
    // one AR/R pair -- so the RAM is multi-channel to match.
    localparam integer MEMP = NROW;           // MAG memory ports
    localparam integer NCH  = 1;       // ports, upload, mover

    wire [MEMP*FW-1:0] mp_in_data, mp_out_data;
    wire [MEMP-1:0]    mp_in_valid, mp_in_busy, mp_out_valid, mp_out_busy;

    genvar gx, gy;
    generate
    for (gx = 0; gx < NCOL; gx = gx + 1) begin : g_col
      for (gy = 0; gy < NROW; gy = gy + 1) begin : g_row
        NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("distributed"),
                    .POS_WIDTH(PW), .POS_X(gx+1), .POS_Y(gy+1),
                    .GRID_LO(1), .GRID_HI(GHI),
                    // Per axis: the grid is no longer square, and clamping x to
                    // the larger of the two would route past the last column.
                    .GRID_X_HI(NCOL), .GRID_Y_HI(NROW)) rtr (
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
    for (gx = 1; gx < NCOL; gx = gx + 1) begin : g_ew
      for (gy = 0; gy < NROW; gy = gy + 1) begin : g_ew_y
        assign w_in[gx][gy] = e_out[gx-1][gy];
        assign w_inv[gx][gy] = e_outv[gx-1][gy];
        assign e_outb[gx-1][gy] = w_inb[gx][gy];
        assign e_in[gx-1][gy] = w_out[gx][gy];
        assign e_inv[gx-1][gy] = w_outv[gx][gy];
        assign w_outb[gx][gy] = e_inb[gx-1][gy];
      end
    end
    for (gx = 0; gx < NCOL; gx = gx + 1) begin : g_ns
      for (gy = 1; gy < NROW; gy = gy + 1) begin : g_ns_y
        assign n_in[gx][gy] = s_out[gx][gy-1];
        assign n_inv[gx][gy] = s_outv[gx][gy-1];
        assign s_outb[gx][gy-1] = n_inb[gx][gy];
        assign s_in[gx][gy-1] = n_out[gx][gy];
        assign s_inv[gx][gy-1] = n_outv[gx][gy];
        assign n_outb[gx][gy] = s_inb[gx][gy-1];
      end
    end

    // ---- dead edges ------------------------------------------------------
    // North of the top row, south of the bottom row, and east of the last
    // column for every row but the agent's. EVERY row's west is a MAG memory
    // port now, so there is no dead west edge left.
    for (gx = 0; gx < NCOL; gx = gx + 1) begin : g_dead_ns
        assign n_in[gx][0] = {FW{1'b0}};
        assign n_inv[gx][0] = 1'b0;  assign n_outb[gx][0] = 1'b0;
        assign s_in[gx][NROW-1] = {FW{1'b0}};
        assign s_inv[gx][NROW-1] = 1'b0;  assign s_outb[gx][NROW-1] = 1'b0;
    end
    // EVERY row's east edge, row 0 included: the agent used to occupy that one.
    for (gy = 0; gy < NROW; gy = gy + 1) begin : g_dead_e
        assign e_in[NCOL-1][gy] = {FW{1'b0}};
        assign e_inv[NCOL-1][gy] = 1'b0;  assign e_outb[NCOL-1][gy] = 1'b0;
    end
    // Locals with no cluster on them: only the tail of the last row, when the
    // count does not fill the grid.
    for (gx = 0; gx < NCOL; gx = gx + 1) begin : g_dead_l
      for (gy = 0; gy < NROW; gy = gy + 1) begin : g_dead_l_y
        localparam integer HAS = ((gy * NCOL + gx) < NCL) ? 1 : 0;
        if (HAS == 0) begin : g_tie
            assign l_in[gx][gy] = {FW{1'b0}};
            assign l_inv[gx][gy] = 1'b0;  assign l_outb[gx][gy] = 1'b0;
        end
      end
    end

    // ---- MAG's ports, one per mesh ROW -----------------------------------
    // Each sits west of its row. DIFFERENT routers on purpose: several ports on
    // one router would split the server and leave the congestion where it was.
    for (gy = 0; gy < MEMP; gy = gy + 1) begin : g_mport
        assign mp_in_data[gy*FW +: FW] = w_out[0][gy];
        assign mp_in_valid[gy]         = w_outv[0][gy];
        assign w_outb[0][gy]           = mp_in_busy[gy];
        assign w_in[0][gy]             = mp_out_data[gy*FW +: FW];
        assign w_inv[0][gy]            = mp_out_valid[gy];
        assign mp_out_busy[gy]         = w_inb[0][gy];
    end
    endgenerate

    // ---------------------------------------------------------------- MAG

    wire [NCH*4-1:0]      r_awid, r_arid, r_bid, r_rid;
    // NCH*40, not NCH*34: mag and axi_ram are ADDR_W=40, and 34 leaves the TOP
    // channel short so its upper address bits are lost.
    wire [NCH*40-1:0]     r_awaddr, r_araddr;
    wire [NCH*8-1:0]      r_awlen, r_arlen;
    wire [NCH*3-1:0]      r_awsize, r_arsize;
    wire [NCH*2-1:0]      r_awburst, r_arburst, r_bresp, r_rresp;
    wire [NCH-1:0]        r_awvalid, r_awready, r_wvalid, r_wready, r_wlast;
    wire [NCH-1:0]        r_bvalid, r_bready, r_arvalid, r_arready;
    wire [NCH-1:0]        r_rvalid, r_rready, r_rlast;
    wire [NCH*DW-1:0]     r_wdata, r_rdata;
    wire [NCH*DW/8-1:0]   r_wstrb;
    wire [15:0]  mag_rd, mag_wr;

    // ---- the upload port: FP16 in, marker on the address ------------------
    // Aperture 0x4/0x5 quantises as it lands, its low bit selecting B packing.
    reg  [39:0]   u_awaddr = 0;
    reg  [7:0]    u_awlen  = 0;
    reg           u_awvalid = 0, u_wvalid = 0, u_wlast = 0, u_bready = 0;
    reg  [DW-1:0] u_wdata = 0;
    wire          u_awready, u_wready, u_bvalid;

    // MAG port p sits west of row p+1, at coordinate (0, p+1). There is no
    // agent node: the agent shares these ports, so MAG attaches to ONE edge.
    // It works because routing clamps the destination into the grid -- a flit
    // for (0,y) walks to router (1,y) and only then takes the outward hop.
    mag #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(40), .ID_W(4),
          .MEM_PORTS(MEMP),
          .MEM_X(0), .MEM_Y(1),
          .MEM_X1(0), .MEM_Y1(2),
          .MEM_X2(0), .MEM_Y2(3),
          .MEM_X3(0), .MEM_Y3(4),
          .GRID_LO(1), .GRID_HI(GHI), .STAGE_FLITS(128)) u_mag (
        .clk(clk), .resetn(rstn),
        .sm_awid(4'd0), .sm_awaddr(u_awaddr), .sm_awlen(u_awlen),
        .sm_awvalid(u_awvalid), .sm_awready(u_awready),
        .sm_wdata(u_wdata), .sm_wstrb({(DW/8){1'b1}}),
        .sm_wlast(u_wlast), .sm_wvalid(u_wvalid), .sm_wready(u_wready),
        .sm_bid(), .sm_bresp(), .sm_bvalid(u_bvalid), .sm_bready(u_bready),
        .sm_arid(4'd0), .sm_araddr(40'd0), .sm_arlen(8'd0), .sm_arvalid(1'b0),
        .sm_arready(), .sm_rid(), .sm_rdata(), .sm_rresp(), .sm_rlast(),
        .sm_rvalid(), .sm_rready(1'b1),
        .sc_awid(s1_awid), .sc_awaddr(s1_awaddr), .sc_awlen(s1_awlen),
        .sc_awvalid(s1_awvalid), .sc_awready(s1_awready),
        .sc_wdata(s1_wdata), .sc_wstrb(s1_wstrb), .sc_wlast(s1_wlast),
        .sc_wvalid(s1_wvalid), .sc_wready(s1_wready),
        .sc_bid(s1_bid), .sc_bresp(s1_bresp), .sc_bvalid(s1_bvalid), .sc_bready(s1_bready),
        .sc_arid(s1_arid), .sc_araddr(s1_araddr), .sc_arlen(s1_arlen),
        .sc_arvalid(s1_arvalid), .sc_arready(s1_arready),
        .sc_rid(s1_rid), .sc_rdata(s1_rdata), .sc_rresp(s1_rresp),
        .sc_rlast(s1_rlast), .sc_rvalid(s1_rvalid), .sc_rready(s1_rready),
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
        .mem_in_data(mp_in_data), .mem_in_valid(mp_in_valid),
        .mem_in_busy(mp_in_busy),
        .mem_out_data(mp_out_data), .mem_out_valid(mp_out_valid),
        .mem_out_busy(mp_out_busy),
        // The agent shares MAG's west ports, so the east edge is dead like
        // every other one -- see g_dead_e below.
        .mem_rd_count(mag_rd), .mem_wr_count(mag_wr),
        // The mover's command path is behind S_AXI_CTRL now, not a sideband --
        // the driver bench does not command it, so only the status is read.
        .mv_busy(), .mv_fault(), .mv_done()
    );

    // ---- per-port taps for the profile -----------------------------------
    // Hierarchical, because these are internal state rather than ports. Aliased
    // into arrays so every counter below SUMS over the ports rather than naming
    // one -- otherwise the profile silently describes port 0 alone.
    wire [3:0] mp_st    [0:MEMP-1];
    wire [1:0] mp_rs    [0:MEMP-1];
    wire       mp_eact  [0:MEMP-1], mp_qrdy   [0:MEMP-1];
    wire       mp_free  [0:MEMP-1], mp_stout  [0:MEMP-1];
    wire       mp_wrb   [0:MEMP-1], mp_bv     [0:MEMP-1];
    wire       mp_rv    [0:MEMP-1], mp_wsfree [0:MEMP-1];

    generate
    for (gy = 0; gy < MEMP; gy = gy + 1) begin : g_tap
        assign mp_st[gy]     = u_mag.g_port[gy].u_eng.st;
        assign mp_rs[gy]     = u_mag.g_port[gy].u_eng.rs;
        assign mp_eact[gy]   = u_mag.g_port[gy].u_eng.e_act;
        assign mp_qrdy[gy]   = u_mag.g_port[gy].u_eng.q_rdy;
        assign mp_free[gy]   = u_mag.g_port[gy].u_eng.out_free;
        assign mp_stout[gy]  = u_mag.g_port[gy].u_eng.st_out;
        assign mp_wrb[gy]    = u_mag.g_port[gy].u_eng.wr_b;
        assign mp_bv[gy]     = u_mag.g_port[gy].u_eng.m_bvalid;
        assign mp_rv[gy]     = u_mag.g_port[gy].u_eng.m_rvalid;
        assign mp_wsfree[gy] = u_mag.g_port[gy].u_eng.ws_has_free;
    end
    endgenerate

    // How many AXI beats and NoC flits moved this cycle, across every channel.
    // A boolean OR would undercount the moment two ports move at once, which
    // is exactly the case the ports were added to create.
    integer nb;
    reg [7:0] n_rdbeat, n_wrbeat, n_nocin, n_nocout;
    always @(*) begin
        n_rdbeat = 8'd0; n_wrbeat = 8'd0; n_nocin = 8'd0; n_nocout = 8'd0;
        for (nb = 0; nb < NCH; nb = nb + 1) begin
            if (r_rvalid[nb] && r_rready[nb]) n_rdbeat = n_rdbeat + 8'd1;
            if (r_wvalid[nb] && r_wready[nb]) n_wrbeat = n_wrbeat + 8'd1;
        end
        for (nb = 0; nb < MEMP; nb = nb + 1) begin
            if (u_mag.mem_in_valid[nb]  && !u_mag.mem_in_busy[nb])
                n_nocin = n_nocin + 8'd1;
            if (u_mag.mem_out_valid[nb] && !u_mag.mem_out_busy[nb])
                n_nocout = n_nocout + 8'd1;
        end
    end

    axi_ram #(.DATA_W(DW), .ADDR_W(40), .ID_W(4), .WORDS(RAM_WORDS),
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
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DW{1'b0}}), .bd_rdata()
    );

    // ---------------------------------------------------------- the clusters
    // One per row. The per-CU state the instrumentation needs is pulled out
    // into arrays here rather than reached for by instance name, so everything
    // below scales with NCL instead of naming cu0 and cu1.
    wire [15:0] cf [0:NCL-1], cg [0:NCL-1], cd [0:NCL-1];
    wire [3:0]  cst [0:NCL-1];
    wire        cgemm [0:NCL-1], cdrain [0:NCL-1], cbusy [0:NCL-1];
    wire        csend_v [0:NCL-1], csend_r [0:NCL-1], crecv_v [0:NCL-1];
    wire        cl1_we [0:NCL-1];

    generate
    for (gy = 0; gy < NCL; gy = gy + 1) begin : g_cu
        // Cluster gy fills the grid row-major, one per router local.
        // `bench.CLUSTERS` lists these coordinates and must agree.
        localparam integer CX   = gy % NCOL;                    // 0-based column
        localparam integer CROW = gy / NCOL + 1;                // 1-based row
        // Its own row's port. Every cluster can reach every port through the
        // mesh, so this only chooses the exit -- and the nearest exit is the one
        // reached without a single north-south hop.
        localparam integer PROW = CROW;
        mx_cluster_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                        .CU_X(CX+1), .CU_Y(CROW),
                        .MEM_X(0), .MEM_Y(PROW),
                        .TILES(512), .GA(128), .GB(256), .MODEL(MODEL)) cu (
            .clk(clk), .resetn(rstn),
            .noc_in_data(l_out[CX][CROW-1]), .noc_in_valid(l_outv[CX][CROW-1]),
            .noc_in_busy(l_outb[CX][CROW-1]),
            .noc_out_data(l_in[CX][CROW-1]), .noc_out_valid(l_inv[CX][CROW-1]),
            .noc_out_busy(l_inb[CX][CROW-1]),
            .fills_done(cf[gy]), .gemms_done(cg[gy]), .drains_done(cd[gy])
        );
        assign cst[gy]     = cu.st;
        assign cgemm[gy]   = cu.gemm_busy;
        assign cdrain[gy]  = cu.drain_busy;
        assign cbusy[gy]   = (cu.st != 4'd0) || cu.gemm_busy || cu.drain_busy;
        assign csend_v[gy] = cu.send_valid;
        assign csend_r[gy] = cu.send_ready;
        assign crecv_v[gy] = cu.recv_valid;
        assign cl1_we[gy]  = cu.l1_we;
    end
    endgenerate

    // A trace of cluster 0's path from memory request to tile write, enabled
    // with `run_matmul.py --dbg`. Every stage is here so a wrong end-to-end
    // result shows WHICH stage first stopped carrying the right thing. Prefer
    // the component benches for anything reproducible there.
`ifdef DBG
    always @(posedge clk) if (rstn) begin
        if (u_mag.g_port[0].u_eng.q_start)
            $display("[%0t] QSTART  ar=%h len=%0d", $time,
                     u_mag.g_port[0].u_eng.m_araddr,
                     u_mag.g_port[0].u_eng.m_arlen);
        if (g_cu[0].cu.l1_we)
            $display("[%0t] L1WE    sel=%0d ent=%0d", $time,
                     g_cu[0].cu.l1_sel, g_cu[0].cu.l1_addr);
        if (g_cu[0].cu.u_node.u_mgr.s1_valid)
            $display("[%0t] ISSUE   tile=%0d first=%b a=%0d b=%0d", $time,
                     g_cu[0].cu.u_node.u_mgr.s1_addr,
                     g_cu[0].cu.u_node.u_mgr.s1_first,
                     g_cu[0].cu.u_node.u_mgr.a_rd, g_cu[0].cu.u_node.u_mgr.b_rd);
        if (g_cu[0].cu.u_node.u_acu.wr_bank_en)
            $display("[%0t] TILEWR  tile=%0d", $time,
                     g_cu[0].cu.u_node.u_acu.wr_addr);
        if (g_cu[0].cu.drain_start)
            $display("[%0t] DRAIN   gemm_busy=%b", $time, g_cu[0].cu.gemm_busy);
        if (u_mag.g_port[0].u_eng.take_wr_data)
            $display("[%0t] WRDATA  %h", $time, u_mag.mem_in_data[63:0]);
    end
`endif

    // Misattributed write data is checked inside MAG ("write data with no open
    // write") and covered directly by mag_wslot_tb, so there is no check here.

    // Occupancy: when was each cluster actually doing something? The control
    // program kicks every cluster before waiting on any, so overlap is as much a
    // property of the PROGRAM as of the hardware, and it decides whether a
    // second cluster buys any throughput at all.
    reg [63:0] busy0_first = 0, busy0_last = 0, busy1_first = 0, busy1_last = 0;
    reg [63:0] both_cycles = 0;
    wire cu0_busy = cbusy[0];
    wire cu1_busy = cbusy[NCL-1];   // the LAST cluster, so overlap spans all

    // ------------------------------------------------- where the cycles go
    // One bucket per CU phase, plus the memory beats that feed them, because
    // event counters say a GEMM happened but not whether the time went to
    // computing or to waiting for operands.
    //
    // S_FREQ/S_FRCV/S_FWR is a FILL, pure operand movement. S_GEMM/S_GWAIT is
    // the array multiplying. S_DRAIN/S_DWAIT is the output tile leaving the
    // accumulator. S_IDLE with a program still running is the CU waiting on the
    // dispatcher -- neither compute nor bandwidth, but real time.
    reg [63:0] fill_cyc = 0, gemm_cyc = 0, drain_cyc = 0, idle_cyc = 0;
    reg [63:0] rd_beats = 0, wr_beats = 0, host_cyc = 0, run_cyc = 0;

    // ------------------------------------------------ operand fetch latency
    // Fill time has two very different causes that look identical in an
    // aggregate: moving too many bytes, or moving them one exposed round trip
    // at a time. These separate them, on any run including the small fast
    // ones, so the question can be answered in seconds rather than inferred
    // from a 512-cube.
    //
    //   lat_first   first request out -> first entry in L1: the round trip
    //   per-entry   steady-state cycles per entry, derived from the total
    //   FILL_FLOOR  cycles per entry at full memory rate (4 response flits)
    //
    // per-entry near the floor means bandwidth-bound: only reuse or wider
    // memory helps. per-entry near lat_first means the CU is serialising its
    // requests and the fix belongs in the CU.
    localparam integer FILL_FLOOR = 4;
    reg [63:0] t_req0 = 0, t_l1_0 = 0, l1_writes = 0;

    always @(posedge clk) if (rstn && measure) begin
        if (csend_v[0] && csend_r[0] && t_req0 == 0) t_req0 <= cycles;
        if (cl1_we[0]) begin
            if (l1_writes == 0) t_l1_0 <= cycles;
            l1_writes <= l1_writes + 64'd1;
        end
    end

    // ------------------------------------------- MAG service FSM occupancy
    // An end-to-end cycle count says the machine is slow; it cannot say which
    // component is holding the others up. MAG serves one NoC memory request
    // at a time, so its state histogram IS the memory system's throughput
    // budget: q_fill is fetching an entry from AXI, q_emit is handing the
    // four operand flits to the NoC, and idle is capacity nobody claimed.
    // Together with the stall counters below this says whether the limit is
    // the requester, the fabric, or MAG itself.
    reg [63:0] m_idle=0, m_qfill=0, m_qwait=0, m_qemit=0, m_rd=0, m_wr=0, m_host=0;
    // Where cycles are LOST rather than spent: a producer with a flit it
    // cannot hand over, or a consumer with nothing to take.
    reg [63:0] s_in_bp=0, s_out_bp=0, s_cu_send=0, s_cu_dry=0;
    // FLITS ACROSS MAG'S MEMORY PORT, both ways. One flit per cycle is the
    // port's whole capacity, so these divided by the run ARE its utilisation --
    // the NoC-side counterpart to counting AXI beats, and the other half of
    // "what fraction of every resource is the machine actually using".
    reg [63:0] noc_in = 0, noc_out = 0;

    // The quantised read has its own state machine and runs CONCURRENTLY with
    // the write and host paths, so these buckets overlap and do NOT sum to the
    // elapsed cycles. That is the measurement: qfill and qemit both high is the
    // fetch/emit overlap working, and summing to 100% means it is not. Summed
    // over the memory ports, so these read as port-cycles.
    integer mq;
    reg [7:0] nm_idle, nm_rd, nm_wr, nm_qfill, nm_qwait, nm_qemit,
              nm_inbp, nm_outbp;
    always @(*) begin
        nm_idle = 8'd0; nm_rd = 8'd0; nm_wr = 8'd0;
        nm_qfill = 8'd0; nm_qwait = 8'd0; nm_qemit = 8'd0;
        nm_inbp = 8'd0; nm_outbp = 8'd0;
        for (mq = 0; mq < MEMP; mq = mq + 1) begin
            if (mp_st[mq] == 4'd0)                  nm_idle  = nm_idle  + 8'd1;
            if (mp_st[mq] == 4'd2)                  nm_rd    = nm_rd    + 8'd1;
            if (mp_st[mq] == 4'd5 || mp_st[mq] == 4'd6)
                                                    nm_wr    = nm_wr    + 8'd1;
            if (mp_rs[mq] == 2'd1)                  nm_qfill = nm_qfill + 8'd1;
            if (mp_rs[mq] == 2'd2)                  nm_qwait = nm_qwait + 8'd1;
            if (mp_eact[mq])                        nm_qemit = nm_qemit + 8'd1;
            if (u_mag.mem_in_busy[mq])              nm_inbp  = nm_inbp  + 8'd1;
            if (u_mag.mem_out_valid[mq] && u_mag.mem_out_busy[mq])
                                                    nm_outbp = nm_outbp + 8'd1;
        end
    end

    always @(posedge clk) if (rstn && measure) begin
        m_idle  <= m_idle  + {56'd0, nm_idle};
        m_rd    <= m_rd    + {56'd0, nm_rd};
        m_wr    <= m_wr    + {56'd0, nm_wr};
        m_qfill <= m_qfill + {56'd0, nm_qfill};
        m_qwait <= m_qwait + {56'd0, nm_qwait};
        m_qemit <= m_qemit + {56'd0, nm_qemit};
        // The host upload is its own machine on its own AXI channel now, so it
        // is counted where it happens rather than as a state of the NoC path.
        if (u_mag.hst != 3'd0) m_host <= m_host + 1;
        // MAG refusing NoC traffic, and MAG unable to hand a response over
        s_in_bp  <= s_in_bp  + {56'd0, nm_inbp};
        s_out_bp <= s_out_bp + {56'd0, nm_outbp};
        // Accepted flits, not offered ones.
        noc_in   <= noc_in   + {56'd0, n_nocin};
        noc_out  <= noc_out  + {56'd0, n_nocout};
        // CU with a request it cannot send, and CU filling with nothing to take
        if (csend_v[0] && !csend_r[0])                  s_cu_send <= s_cu_send + 1;
        if ((cst[0] == 4'd1) && !crecv_v[0])            s_cu_dry  <= s_cu_dry  + 1;
    end

    // ---------------------------------------- WHY, not merely WHERE
    // A state histogram says S_WR_ACK was 95% occupied, not whether that was
    // work or waiting -- and only the second is a fault. Every counter below is
    // a state PLUS the reason it could not advance, so a regression names its
    // own cause instead of needing a bisect. `wack_nob` is the concrete case:
    // cycles in S_WR_ACK with no B response in hand and none arriving, which is
    // what a read emitter stealing the single cycle m_bready consumed it on
    // looks like.
    reg [63:0] wack_nob = 0, wack_blk = 0, wslot_full = 0;
    reg [63:0] rfill_dry = 0, rwait_emit = 0, emit_bp = 0;
    reg [63:0] cu0_dwait = 0, cu0_gwait = 0;

    integer wq;
    reg [7:0] n_wnob, n_wblk, n_wsfull, n_rdry, n_rwemit, n_ebp;
    always @(*) begin
        n_wnob = 8'd0; n_wblk = 8'd0; n_wsfull = 8'd0;
        n_rdry = 8'd0; n_rwemit = 8'd0; n_ebp = 8'd0;
        for (wq = 0; wq < MEMP; wq = wq + 1) begin
            if (mp_st[wq] == 4'd6) begin
                if (!(mp_bv[wq] || mp_wrb[wq])) n_wnob = n_wnob + 8'd1;
                else if (!mp_stout[wq])         n_wblk = n_wblk + 8'd1;
            end
            // No free write slot: intake cannot accept another descriptor,
            // which is how a stalled write path becomes a jammed input queue.
            if (!mp_wsfree[wq])                 n_wsfull = n_wsfull + 8'd1;
            // Read engine: starved by memory, or held up by the emit buffer.
            // The second says fetch/emit overlap has stopped being the win.
            if ((mp_rs[wq] == 2'd1) && !mp_rv[wq])
                                                n_rdry = n_rdry + 8'd1;
            if ((mp_rs[wq] == 2'd2) && mp_qrdy[wq] && mp_eact[wq])
                                                n_rwemit = n_rwemit + 8'd1;
            // Emitter holding a flit the mesh will not take.
            if (mp_eact[wq] && !mp_free[wq])    n_ebp = n_ebp + 8'd1;
        end
    end

    always @(posedge clk) if (rstn && measure) begin
        wack_nob   <= wack_nob   + {56'd0, n_wnob};
        wack_blk   <= wack_blk   + {56'd0, n_wblk};
        wslot_full <= wslot_full + {56'd0, n_wsfull};
        rfill_dry  <= rfill_dry  + {56'd0, n_rdry};
        rwait_emit <= rwait_emit + {56'd0, n_rwemit};
        emit_bp    <= emit_bp    + {56'd0, n_ebp};
        // CU tails: DRAIN waiting for its last write to leave, GEMM waiting
        // for the cascade to settle. Both are per-instruction overhead that no
        // end-to-end number attributes.
        if (cst[0] == 4'd6) cu0_dwait <= cu0_dwait + 1;
        if (cst[0] == 4'd4) cu0_gwait <= cu0_gwait + 1;
    end

    // Progress watchdog: everything the machine retires is counted here, so if
    // none of it moves nothing is happening. A SUM, not a concatenation --
    // every term is monotonic, so any one of them moving moves the sum, and
    // unlike a concatenation it does not grow a field per cluster.
    integer pw;
    reg [63:0] progress;
    always @(*) begin
        progress = {48'd0, mag_rd} + {48'd0, mag_wr};
        for (pw = 0; pw < NCL; pw = pw + 1)
            progress = progress + {48'd0, cf[pw]} + {48'd0, cg[pw]}
                                + {48'd0, cd[pw]};
    end
    reg  [63:0] prog_prev = 0;
    reg  [31:0]  stall = 0;

    // Cleared at every GO. Between rounds the host is uploading the next
    // round's flits and NOTHING retires -- that is the design working, not a
    // stall -- so a counter left running across the gap arrives at the next
    // wait already expired and kills a healthy round instantly.
    reg stall_clr = 1'b0;

    always @(posedge clk) if (rstn) begin
        if (stall_clr || progress !== prog_prev) begin
            prog_prev <= progress;
            stall     <= 32'd0;
        end else begin
            stall <= stall + 32'd1;
        end
    end

    // S_FILL is now ONE state (4'd1): the requester and receiver run together
    // inside it. It used to be three -- S_FREQ/S_FRCV/S_FWR, 1/2/8 -- which is
    // what a stale copy of this function would still be testing.
    function is_fill;  input [3:0] s; is_fill  = (s==4'd1);
    endfunction
    function is_gemm;  input [3:0] s; is_gemm  = (s==4'd3)||(s==4'd4);
    endfunction
    function is_drain; input [3:0] s; is_drain = (s==4'd5)||(s==4'd6);
    endfunction

    // How many clusters are in each phase THIS cycle. Counted combinationally
    // and added once, because two separate `if`s targeting one reg inside an
    // always block do not both apply -- the later wins, so a cycle with two
    // clusters filling would have counted as one.
    integer pc;
    reg [7:0] n_fill, n_gemm, n_drain, n_idle;
    always @(*) begin
        n_fill = 8'd0; n_gemm = 8'd0; n_drain = 8'd0; n_idle = 8'd0;
        for (pc = 0; pc < NCL; pc = pc + 1) begin
            if (is_fill(cst[pc]))                     n_fill  = n_fill  + 8'd1;
            if (is_gemm(cst[pc])  || cgemm[pc])       n_gemm  = n_gemm  + 8'd1;
            if (is_drain(cst[pc]) || cdrain[pc])      n_drain = n_drain + 8'd1;
            if (!cbusy[pc])                           n_idle  = n_idle  + 8'd1;
        end
    end

    // ------------------------------------------- the working pipeline, in time
    // A histogram says how MUCH time each phase took, never WHEN -- and whether
    // a fill hid inside the previous sweep is not derivable from two totals.
    //
    // Sampled, not logged: one nibble per cluster per TRACE_GAP cycles. The
    // nibble is a MASK, not a state, because the phases genuinely overlap and a
    // sample forced to pick one would hide what this exists to show. ORed
    // across the window, so a phase shorter than the gap still appears.
    localparam integer TRACE_GAP = 64;
    localparam integer TRACE_MAX = 2048;
    reg [3:0]  tr     [0:NCL-1][0:TRACE_MAX-1];
    reg [3:0]  tr_acc [0:NCL-1];
    reg [31:0] tr_n = 0;
    reg [31:0] tr_c = 0;
    integer ti;

    initial begin
        for (ti = 0; ti < NCL; ti = ti + 1) tr_acc[ti] = 4'd0;
    end

    always @(posedge clk) if (rstn && measure) begin
        for (ti = 0; ti < NCL; ti = ti + 1)
            tr_acc[ti] <= tr_acc[ti]
                        | {!cbusy[ti],
                           is_drain(cst[ti]) || cdrain[ti],
                           is_gemm(cst[ti])  || cgemm[ti],
                           is_fill(cst[ti])};
        if (tr_c == TRACE_GAP - 1) begin
            tr_c <= 0;
            if (tr_n < TRACE_MAX) begin
                for (ti = 0; ti < NCL; ti = ti + 1) begin
                    tr[ti][tr_n] <= tr_acc[ti]
                                  | {!cbusy[ti],
                                     is_drain(cst[ti]) || cdrain[ti],
                                     is_gemm(cst[ti])  || cgemm[ti],
                                     is_fill(cst[ti])};
                    tr_acc[ti] <= 4'd0;
                end
                tr_n <= tr_n + 32'd1;
            end
        end else tr_c <= tr_c + 32'd1;
    end

    always @(posedge clk) if (rstn && measure) begin
        // Counted per CU, so on a 2-cluster machine the buckets sum to twice
        // the elapsed cycles when both are busy. That is the point -- it is
        // occupancy, not wall clock.
        // One assignment per counter. Two separate `if`s targeting the same
        // reg in one always block do NOT both apply -- the later wins -- so a
        // cycle where both CUs were filling would have counted as one.
        fill_cyc  <= fill_cyc  + {56'd0, n_fill};
        gemm_cyc  <= gemm_cyc  + {56'd0, n_gemm};
        drain_cyc <= drain_cyc + {56'd0, n_drain};
        idle_cyc  <= idle_cyc  + {56'd0, n_idle};

        // DW-bit beats on the memory channels: what the operands actually cost
        // in bandwidth, independent of how long the CU held them. Summed over
        // channels, because read and write are separate AXI channels AND there
        // is now more than one of each -- so these are two independent budgets
        // of NCH beats per cycle, never one of 2*NCH.
        rd_beats <= rd_beats + {56'd0, n_rdbeat};
        wr_beats <= wr_beats + {56'd0, n_wrbeat};
    end

    always @(posedge clk) if (rstn && measure) begin
        if (cu0_busy) begin
            if (busy0_first == 0) busy0_first <= $time;
            busy0_last <= $time;
        end
        if (cu1_busy) begin
            if (busy1_first == 0) busy1_first <= $time;
            busy1_last <= $time;
        end
        if (cu0_busy && cu1_busy) both_cycles <= both_cycles + 1;
    end

    // ============================================================ AXI tasks
    // Host AXI, with every handshake BOUNDED. An unbounded `while (!ready)` is
    // invisible when it never completes: the upload runs before GO, so no round
    // limit covers it and a stall looks exactly like a slow simulation.
    //
    // A MACRO, not a task: a task's input is sampled once at call time, so
    // `while (!cond)` on a task argument spins on the value the signal had at
    // entry and never observes it going high.
    localparam integer HS_LIMIT = 100000;   // cycles allowed per handshake
    integer hs;
    reg     hs_bad = 1'b0;

`define AWAIT(sig, what)                                                      \
    hs = 0;                                                                   \
    while (!(sig) && hs < HS_LIMIT) begin @(posedge clk); hs = hs + 1; end     \
    if (hs >= HS_LIMIT && !hs_bad) begin                                      \
        hs_bad = 1'b1;                                                        \
        $display("  ERROR host AXI stalled on %0s at %0t", what, $time);      \
    end

    task drv_write(input [31:0] a, input [63:0] d);
        begin
            @(posedge clk);
            d_awaddr <= a; d_awlen <= 0; d_awid <= 4'h1; d_awvalid <= 1'b1;
            @(posedge clk); `AWAIT(d_awready, "awready")
            d_awvalid <= 1'b0;
            d_wdata <= d; d_wlast <= 1'b1; d_wvalid <= 1'b1; d_bready <= 1'b1;
            @(posedge clk); `AWAIT(d_wready, "wready")
            d_wvalid <= 1'b0; d_wlast <= 1'b0;
            @(posedge clk); `AWAIT(d_bvalid, "bvalid")
            d_bready <= 1'b0;
        end
    endtask

    task drv_read(input [31:0] a, output [63:0] d);
        begin
            @(posedge clk);
            d_araddr <= a; d_arlen <= 0; d_arid <= 4'h2; d_arvalid <= 1'b1;
            @(posedge clk); `AWAIT(d_arready, "arready")
            d_arvalid <= 1'b0; d_rready <= 1'b1;
            @(posedge clk); `AWAIT(d_rvalid, "rvalid")
            d = d_rdata;
            @(posedge clk); d_rready <= 1'b0;
        end
    endtask

    // ---------------------------------------------------- the operand upload
    // One AXI burst on MAG's memory window. Every handshake is spun on the
    // EDGE that carries it -- `@(posedge clk)` returns before the non-blocking
    // updates, so the value read there is the one the slave sampled.
    reg [DW-1:0] hostimg [0:MAX_HOST-1];      // the host's FP16 image
    reg [127:0]  ups     [0:MAX_UPLOADS-1];   // {src, dst, words, flags}
    reg [63:0] up_beats = 0;
    integer    up_b;
    reg [31:0] up_n;

    task host_burst(input integer src, input [39:0] dst, input integer nb,
                    input quant, input blay);
        begin
            up_n      = nb;
            // {special, rsvd, mesh, aperture 0b010, blay} then 4 GB of target.
            u_awaddr  <= quant ? {4'b1000, 3'b010, blay, dst[31:0]}
                               : {8'd0, dst[31:0]};
            u_awlen   <= up_n[7:0] - 8'd1;
            u_awvalid <= 1'b1;
            u_bready  <= 1'b1;
            @(posedge clk); `AWAIT(u_awready, "sm_awready")
            u_awvalid <= 1'b0;
            for (up_b = 0; up_b < up_n; up_b = up_b + 1) begin
                u_wdata  <= hostimg[src + up_b];
                u_wlast  <= (up_b == up_n - 1);
                u_wvalid <= 1'b1;
                @(posedge clk); `AWAIT(u_wready, "sm_wready")
                up_beats <= up_beats + 64'd1;
            end
            u_wvalid <= 1'b0; u_wlast <= 1'b0;
            @(posedge clk); `AWAIT(u_bvalid, "sm_bvalid")
            u_bready <= 1'b0;
        end
    endtask

    // A region, cut into bursts. Two constraints, and they differ by kind: a
    // quantising upload must carry whole 8-beat entries (MAG makes its own
    // 128-byte destination bursts, which can never straddle 4 KB); a verbatim
    // upload is forwarded beat for beat, so its burst must not cross a 4 KB
    // boundary in the destination.
    task host_region(input integer src, input integer dst, input integer nw,
                     input quant, input blay);
        integer left, s, d, nb, room;
        begin
            left = nw; s = src; d = dst;
            while (left > 0) begin
                nb = (left > UP_BEATS) ? UP_BEATS : left;
                if (!quant) begin
                    room = (4096 - ((d * (DW/8)) % 4096)) / (DW/8);
                    if (nb > room) nb = room;
                end
                host_burst(s, d * (DW/8), nb, quant, blay);
                s    = s + nb;
                d    = d + (quant ? (nb / 2) : nb);
                left = left - nb;
            end
        end
    endtask

    // ============================================================ the run
    // Cycles between host polls of MO_CTRL. Big enough that the poll loop is
    // not a load on the fabric, small enough that it adds no real latency.
    localparam integer POLL_GAP = 64;
    // A round is stuck when it stops making PROGRESS, not when it exceeds some
    // elapsed time: a size-derived limit has to be re-guessed per problem, and
    // guessing high turns a wedge into a quarter-hour of silence.
    //
    // The bound is the longest a HEALTHY machine goes without any counter
    // moving -- one GEMM instruction, ~600 cycles, since nothing else retires
    // while the array runs. 20000 is ~30x that and does not depend on M, N, K,
    // the round count or the cluster count.
    localparam integer STALL_LIMIT = 20000;


    reg [255:0] prog   [0:MAX_CMDS-1];    // {mask, data, addr, op}
    reg [127:0] setup  [0:MAX_SETUP-1];   // {addr[63:0], data[63:0]}
    reg [127:0] rounds [0:MAX_ROUNDS-1];  // {nsetup, ncmd} per round
    reg [63:0]  rv;
    integer     i, f, r, nrounds, soff, coff, ns, nc, ok, nup;
    reg [63:0]  up_cyc = 0;

    // How the run ends. Under `SERVER the simulation is meant to be reloaded
    // and run again, so it must NOT exit the process: $stop hands control back
    // to the xsim Tcl prompt, where `restart` re-executes the initial blocks
    // and picks up freshly written operand and program files. $finish would
    // take the whole snapshot down and force a ~10 s re-elaboration per run.
    task sim_end;
        begin
            $writememh("result.hex", u_ram.mem);
`ifdef SERVER
            $stop;
`else
            $finish;
`endif
        end
    endtask

    initial begin
        for (i = 0; i < MAX_CMDS;    i = i + 1) prog[i]   = 256'd0;
        for (i = 0; i < MAX_SETUP;   i = i + 1) setup[i]  = 128'd0;
        for (i = 0; i < MAX_ROUNDS;  i = i + 1) rounds[i] = 128'd0;
        for (i = 0; i < MAX_UPLOADS; i = i + 1) ups[i]    = 128'd0;
        // Device memory starts EMPTY. It used to be initialised by the same
        // $readmemh that loaded the operands, so a restart in server mode also
        // wiped the previous run's results; now that operands arrive over AXI,
        // clearing it is what stops a stale C from looking like a fresh one.
        for (i = 0; i < RAM_WORDS;   i = i + 1) u_ram.mem[i] = {DW{1'b0}};
        $readmemh("operands.hex", hostimg);
        $readmemh("uploads.hex", ups);
        $readmemh("setup.hex", setup);
        $readmemh("program.hex", prog);
        $readmemh("rounds.hex", rounds);

        #200; repeat (10) @(posedge clk); rstn = 1; repeat (10) @(posedge clk);

        nrounds = 0;
        for (i = 0; i < MAX_ROUNDS; i = i + 1)
            if (rounds[i][63:0] != 64'd0) nrounds = i + 1;

        // ---- upload the operands, exactly as a host would ----------------
        // Before any round: FP16 over the memory window, with the quantise
        // marker on the regions the driver said were pre-quantised. Counted
        // separately from `host`, because this is paid once per TENSOR while
        // the setup writes are paid once per round.
        nup = 0;
        for (i = 0; i < MAX_UPLOADS; i = i + 1)
            if (ups[i][63:32] != 32'd0) nup = i + 1;
        cyc0 = cycles;
        for (i = 0; i < nup; i = i + 1)
            host_region(ups[i][127:96], ups[i][95:64], ups[i][63:32],
                        ups[i][0], ups[i][1]);
        up_cyc = cycles - cyc0;
        measure = 1'b1;
        $display("--- upload %0d regions, %0d beats, %0d cycles ---",
                 nup, up_beats, up_cyc);

        // ROUNDS. A problem larger than the staging window and command RAM is
        // cut into self-contained upload / load / GO steps and streamed, so
        // the card never holds the whole program at once. Results accumulate
        // in memory between rounds, which is why nothing is reset here.
        $display("--- %0d rounds ---", nrounds);
        soff = 0; coff = 0; ok = 1;
        for (r = 0; r < nrounds; r = r + 1) begin
            ns = rounds[r][127:64];
            nc = rounds[r][63:0];

            // Setup as ordinary host writes: instruction flits are data the
            // host puts in the card's staging RAM, not program. Through the
            // command RAM they would be copied twice and the program would
            // grow with the problem instead of with its control flow.
            cyc0 = cycles;
            for (i = 0; i < ns; i = i + 1)
                drv_write(setup[soff + i][127:64], setup[soff + i][63:0]);
            for (i = 0; i < nc; i = i + 1)
                for (f = 0; f < 4; f = f + 1)
                    drv_write(ORC_BASE + MO_CMD + i*32 + f*8,
                              prog[coff + i][f*64 +: 64]);
            host_cyc = host_cyc + (cycles - cyc0);

            drv_write(ORC_BASE + MO_CTRL, 64'd1);
            stall_clr = 1'b1; @(posedge clk); stall_clr = 1'b0;
            rv = 64'd0; cyc0 = cycles;
            // Exits as soon as done is seen -- at most POLL_GAP cycles after
            // the machine raises it -- and gives up STALL_LIMIT cycles after
            // the machine stops retiring anything. Neither bound grows with
            // the problem, so a 512-cube and a 64-cube report a wedge equally
            // fast.
            while (!rv[1] && stall < STALL_LIMIT) begin
                // BACK OFF between polls: every drv_read is a full AXI
                // transaction through the crossbar and the orchestrator is
                // already spinning on SIG_DONE on the same bus. Two masters
                // hammering the fabric is work the SIMULATOR evaluates every
                // cycle, and it dominated the runtime of a large GEMM. A real
                // host polls a PCIe BAR in microseconds, so the gap is the
                // faithful model as well as the fast one.
                repeat (POLL_GAP) @(posedge clk);
                drv_read(ORC_BASE + MO_CTRL, rv);
            end
            run_cyc = run_cyc + (cycles - cyc0);
            drv_read(ORC_BASE + MO_CODE, rv);
            if (rv[15:0] !== 16'hC0DE) begin
                // Everything needed to place the stall, printed at the moment
                // it is detected. The command index says WHICH control step
                // never retired; the CU and credit state say why.
                drv_read(ORC_BASE + MO_PC, rv);
                $display("  ROUND-FAIL round %0d stopped at pc=%0d", r, rv[15:0]);
                $display("      after %0d cycles, %0d of %0d commands, %0d idle",
                         cycles - cyc0, rv[15:0], nc, stall);
                $display("      cu0 f=%0d g=%0d d=%0d  cu1 f=%0d g=%0d d=%0d",
                         cf[0], cg[0], cd[0],
                         cf[NCL-1], cg[NCL-1], cd[NCL-1]);
                $display("      mem rd=%0d wr=%0d", mag_rd, mag_wr);
                drv_read(MAG_BASE + A_SIG_DONE, rv);
                $display("      SIG_DONE=%0d  (round expects %0d)", rv[15:0],
                         ns / 5);
                drv_read(MAG_BASE + A_PROG_STAT, rv);
                $display("      PROG_STAT run=%0d left=%0d cred=%0d",
                         rv[0], rv[16:1], rv[32:17]);
                ok = 0;
                r = nrounds;
            end
            soff = soff + ns;
            coff = coff + nc;
        end

        // cu0 and the LAST cluster, which is what `sim.py` parses. With NCL = 2
        // they are the same two as before.
        $display("    cu0 f=%0d g=%0d d=%0d  cu1 f=%0d g=%0d d=%0d  mem rd=%0d wr=%0d",
                 cf[0], cg[0], cd[0], cf[NCL-1], cg[NCL-1], cd[NCL-1],
                 mag_rd, mag_wr);
        repeat (400) @(posedge clk);
        $display("    OCCUPANCY cu0 %0t..%0t  cu1 %0t..%0t  overlap %0d cycles",
                 busy0_first, busy0_last, busy1_first, busy1_last, both_cycles);
        $display("    PROFILE host=%0d run=%0d fill=%0d gemm=%0d drain=%0d idle=%0d rd=%0d wr=%0d beat=%0d",
                 host_cyc, run_cyc, fill_cyc, gemm_cyc, drain_cyc, idle_cyc,
                 rd_beats, wr_beats, DW/8);
        $display("    FETCH lat=%0d entries=%0d floor=%0d",
                 t_l1_0 - t_req0, l1_writes, FILL_FLOOR);
        $display("    UPLOAD regions=%0d beats=%0d cycles=%0d",
                 nup, up_beats, up_cyc);
        // The port count travels WITH the counts. These are sums over ports
        // and channels, so a utilisation derived from them without knowing how
        // many there are reads straight past 100% -- which it did.
        $display("    NOC in=%0d out=%0d ports=%0d chans=%0d",
                 noc_in, noc_out, MEMP, NCH);
        $display("    MAGSTATE idle=%0d qfill=%0d qwait=%0d qemit=%0d rd=%0d wr=%0d host=%0d",
                 m_idle, m_qfill, m_qwait, m_qemit, m_rd, m_wr, m_host);
        $display("    STALL in_bp=%0d out_bp=%0d cu_send=%0d cu_dry=%0d",
                 s_in_bp, s_out_bp, s_cu_send, s_cu_dry);
        $display("    WHY wack_nob=%0d wack_blk=%0d wslot_full=%0d rfill_dry=%0d rwait_emit=%0d emit_bp=%0d cu_dwait=%0d cu_gwait=%0d",
                 wack_nob, wack_blk, wslot_full, rfill_dry, rwait_emit,
                 emit_bp, cu0_dwait, cu0_gwait);
        // One line per cluster: a hex nibble per sample, bit0 fill, bit1 gemm,
        // bit2 drain, bit3 idle. Printed last so a parser can stop at the
        // first TRACE line without scanning past the counters.
        for (i = 0; i < NCL; i = i + 1) begin
            $write("    TRACE cu%0d gap=%0d ", i, TRACE_GAP);
            for (f = 0; f < tr_n; f = f + 1) $write("%h", tr[i][f]);
            $write("\n");
        end
        if (ok) $display("  ORCH-OK");
        else    $display("  ORCH-FAIL");
        sim_end;
    end

    initial begin
        repeat (WDOG_CYCLES) @(posedge clk);
        $display("  WATCHDOG after %0d cycles -- cu0 f=%0d g=%0d d=%0d  cu1 f=%0d g=%0d d=%0d  mem rd=%0d wr=%0d",
                 WDOG_CYCLES, cf[0], cg[0], cd[0],
                 cf[NCL-1], cg[NCL-1], cd[NCL-1], mag_rd, mag_wr);
        sim_end;
    end

endmodule

`default_nettype wire
