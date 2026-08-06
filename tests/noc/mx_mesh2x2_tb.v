// The whole machine, small: orchestrator + 2x2 NoC + TWO clusters + memory.
//
//          x=0    x=1    x=2    x=3
//   y=0     .      .      .      .
//   y=1    ORC -- [R] -- [R] --  .        [R] = router
//                  |      |               ORC / MEM hang off WEST edge ports
//                 mgr0   acu0
//   y=2    MEM -- [R] -- [R] --  .
//                  |      |
//                 mgr1   acu1
//   y=3     .      .      .      .
//
// Four routers at x,y in {1,2}; GRID_LO=1, GRID_HI=2.
//
// EACH CLUSTER SPANS TWO ROUTERS. The manager sits on one router's local port
// and the accumulator on a DIFFERENT router's local port -- not two ports of
// the same router. Sharing a router would put operand fetch and result
// write-back through one set of buffers and one arbiter, which is the
// contention the two-port split exists to avoid.
//
// The orchestrator and memory are off-grid coordinates on west edge ports.
// That works because the router routes to the CLAMPED destination first and
// only then uses the unclamped coordinate to pick an edge port, so a packet
// for (0,2) travels to router (1,2) and exits west -- see noc_inport.v.
//
// Both clusters fetch their own operands and write their own results; the
// orchestrator only dispatches instructions and collects completion signals.
//
// The problem is split by OUTPUT, not by K -- cluster c computes its own
// column half, sweeps the whole K itself, and never needs a peer reduction.
// See docs/compute/tensor-isa.md: K inside a cluster is free, splitting K
// across clusters costs a reduction that dominates the compute.
//
//   C[32,64] = A[32,128] x B[128,64]      K = 128 = 4 quantisation blocks
//     cluster 0 -> C[:, 0:32]     cluster 1 -> C[:, 32:64]
//
// K > 32 matters: with a single block every accumulator command is a LOAD, so
// the cross-block accumulate path is never exercised. Four blocks means
// LOAD then three ADDs into each of the 64 resident sub-tiles, with a
// different E8M0 scale pair per block.
//
// Checked against FP64 and exact-MXFP7, with the two ground truths asserted
// equal to each other first.

`default_nettype none
`timescale 1ns/1ps

`ifndef MX_MODEL
`define MX_MODEL 1
`endif

module mx_mesh2x2_tb;

    localparam integer FW    = 288;
    localparam integer PW    = 4;
    localparam integer DW    = 64;
    localparam integer WORDS = 5;
    localparam integer MODEL = `MX_MODEL;

    localparam integer M  = 32, N = 64, KK = 128;
    localparam integer NKB = KK/32;         // 4 K blocks -> exercises accumulate
    localparam integer GM = M/4;            // 8 row groups
    localparam integer GNC = (N/2)/4;       // 8 column groups per cluster
    localparam integer NT = GM*GNC;         // 64 sub-tiles per cluster
    localparam integer NEA = GM*NKB;        // 32 A entries in L1
    localparam integer NEB = GNC*NKB;       // 32 B entries in L1

    // memory word map (256-bit words); one L1 entry is 4 consecutive words
    localparam integer WA_BASE  = 0;        // 32 entries x 4 = 128 words
    localparam integer WB0_BASE = 128;      // 128 words
    localparam integer WB1_BASE = 256;      // 128 words
    localparam integer WC0_BASE = 512;      // 64 words
    localparam integer WC1_BASE = 640;

    localparam [31:0] A_PROG_DST = 32'h0040, A_PROG_LEN = 32'h0048,
                      A_PROG_KICK= 32'h0050, A_PROG_CRED= 32'h0060,
                      A_NODE     = 32'h1000, A_STAGE    = 32'h2000;

    reg clk = 0, rstn = 0;
    always #2 clk = ~clk;

    // ------------------------------------------------------------ AXI
    reg  [3:0]  awid, arid;
    reg  [31:0] awaddr, araddr;
    reg  [7:0]  awlen, arlen;
    reg         awvalid, arvalid, wvalid, wlast, bready, rready;
    reg  [63:0] wdata;
    wire        awready, wready, bvalid, arready, rvalid, rlast;
    wire [63:0] rdata;
    wire [3:0]  bid, rid;
    wire [1:0]  bresp, rresp;

    // ---------------------------------------------------- mesh 2x2 wires
    // r[x][y]; local port serves the node at that coordinate.
    wire [FW-1:0] l_in  [0:1][0:1], l_out [0:1][0:1];
    wire          l_inv [0:1][0:1], l_outv[0:1][0:1];
    wire          l_inb [0:1][0:1], l_outb[0:1][0:1];
    wire [FW-1:0] n_in  [0:1][0:1], n_out [0:1][0:1];
    wire          n_inv [0:1][0:1], n_outv[0:1][0:1];
    wire          n_inb [0:1][0:1], n_outb[0:1][0:1];
    wire [FW-1:0] e_in  [0:1][0:1], e_out [0:1][0:1];
    wire          e_inv [0:1][0:1], e_outv[0:1][0:1];
    wire          e_inb [0:1][0:1], e_outb[0:1][0:1];
    wire [FW-1:0] w_in  [0:1][0:1], w_out [0:1][0:1];
    wire          w_inv [0:1][0:1], w_outv[0:1][0:1];
    wire          w_inb [0:1][0:1], w_outb[0:1][0:1];
    wire [FW-1:0] s_in  [0:1][0:1], s_out [0:1][0:1];
    wire          s_inv [0:1][0:1], s_outv[0:1][0:1];
    wire          s_inb [0:1][0:1], s_outb[0:1][0:1];

    // array index [gx][gy] maps to router coordinate (gx+1, gy+1)
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
    endgenerate

    // east/west neighbours: (0,y) east <-> (1,y) west
    generate
    for (gy = 0; gy < 2; gy = gy + 1) begin : g_ew
        assign w_in[1][gy]  = e_out[0][gy];
        assign w_inv[1][gy] = e_outv[0][gy];
        assign e_outb[0][gy] = w_inb[1][gy];
        assign e_in[0][gy]  = w_out[1][gy];
        assign e_inv[0][gy] = w_outv[1][gy];
        assign w_outb[1][gy] = e_inb[0][gy];
    end
    endgenerate

    // south/north neighbours: (x,0) south <-> (x,1) north
    generate
    for (gx = 0; gx < 2; gx = gx + 1) begin : g_ns
        assign n_in[gx][1]  = s_out[gx][0];
        assign n_inv[gx][1] = s_outv[gx][0];
        assign s_outb[gx][0] = n_inb[gx][1];
        assign s_in[gx][0]  = n_out[gx][1];
        assign s_inv[gx][0] = n_outv[gx][1];
        assign n_outb[gx][1] = s_inb[gx][0];
    end
    endgenerate

    // ---- edge ports, tied off ONE BY ONE ------------------------------
    // Written out explicitly rather than in a loop. A generate that tied off
    // "all north ports" once also drove a vertical mesh link -- two continuous
    // drivers on one wire, so the link resolved to X. One cluster never
    // noticed; the other routed straight through it and hung, silently.
    //
    // West edges carry the orchestrator (1,1) and memory (1,2), so only the
    // remaining six edges are dead.
    assign e_in[1][0] = {FW{1'b0}}; assign e_inv[1][0] = 1'b0; assign e_outb[1][0] = 1'b0;
    assign e_in[1][1] = {FW{1'b0}}; assign e_inv[1][1] = 1'b0; assign e_outb[1][1] = 1'b0;
    assign n_in[0][0] = {FW{1'b0}}; assign n_inv[0][0] = 1'b0; assign n_outb[0][0] = 1'b0;
    assign n_in[1][0] = {FW{1'b0}}; assign n_inv[1][0] = 1'b0; assign n_outb[1][0] = 1'b0;
    assign s_in[0][1] = {FW{1'b0}}; assign s_inv[0][1] = 1'b0; assign s_outb[0][1] = 1'b0;
    assign s_in[1][1] = {FW{1'b0}}; assign s_inv[1][1] = 1'b0; assign s_outb[1][1] = 1'b0;

    // ------------------------------------------------------- orchestrator (0,0)
    noc_orchestrator #(.DATA_WIDTH(DW), .ADDR_WIDTH(32), .ID_WIDTH(4),
                       .FLIT_WIDTH(FW), .POS_WIDTH(PW),
                       .GRID_LO(1), .GRID_HI(2),
                       .ORC_X(0), .ORC_Y(1), .STAGE_FLITS(128)) orc (
        .clk(clk), .resetn(rstn),
        .s_axi_awid(awid), .s_axi_awaddr(awaddr), .s_axi_awlen(awlen),
        .s_axi_awsize(3'd3), .s_axi_awburst(2'b01), .s_axi_awvalid(awvalid),
        .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(8'hFF), .s_axi_wlast(wlast),
        .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bid(bid), .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_arid(arid), .s_axi_araddr(araddr), .s_axi_arlen(arlen),
        .s_axi_arsize(3'd3), .s_axi_arburst(2'b01), .s_axi_arvalid(arvalid),
        .s_axi_arready(arready),
        .s_axi_rid(rid), .s_axi_rdata(rdata), .s_axi_rresp(rresp),
        .s_axi_rlast(rlast), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        // west edge of router (1,1)
        .noc_out_data(w_in[0][0]), .noc_out_valid(w_inv[0][0]), .noc_out_busy(w_inb[0][0]),
        .noc_in_data(w_out[0][0]), .noc_in_valid(w_outv[0][0]), .noc_in_busy(w_outb[0][0])
    );

    // ------------------------------------------------------- fake memory (0,1)
    reg          bd_we;
    reg  [15:0]  bd_addr;
    reg  [255:0] bd_wdata;
    wire [255:0] bd_rdata;
    wire [15:0]  mem_rd, mem_wr;

    noc_fake_mem #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                   .POS_X(0), .POS_Y(2), .WORDS(1024)) fmem (
        .clk(clk), .resetn(rstn),
        // west edge of router (1,2)
        .noc_in_data(w_out[0][1]), .noc_in_valid(w_outv[0][1]), .noc_in_busy(w_outb[0][1]),
        .noc_out_data(w_in[0][1]), .noc_out_valid(w_inv[0][1]), .noc_out_busy(w_inb[0][1]),
        .bd_we(bd_we), .bd_addr(bd_addr), .bd_wdata(bd_wdata), .bd_rdata(bd_rdata),
        .rd_count(mem_rd), .wr_count(mem_wr)
    );

    // ------------------------------------------------------- clusters
    // Each cluster spans TWO routers, one endpoint on each local port:
    //     cluster 0   manager (1,1) local    accumulator (2,1) local
    //     cluster 1   manager (1,2) local    accumulator (2,2) local
    wire [15:0] f0, g0, d0, f1, g1, d1;

    mx_cluster_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                    .MGR_X(1), .MGR_Y(1), .ACU_X(2), .ACU_Y(1),
                    .MEM_X(0), .MEM_Y(2),
                    .TILES(256), .GA(32), .GB(32), .MODEL(MODEL)) cu0 (
        .clk(clk), .resetn(rstn),
        .m_in_data(l_out[0][0]), .m_in_valid(l_outv[0][0]), .m_in_busy(l_outb[0][0]),
        .m_out_data(l_in[0][0]), .m_out_valid(l_inv[0][0]), .m_out_busy(l_inb[0][0]),
        .a_in_data(l_out[1][0]), .a_in_valid(l_outv[1][0]), .a_in_busy(l_outb[1][0]),
        .a_out_data(l_in[1][0]), .a_out_valid(l_inv[1][0]), .a_out_busy(l_inb[1][0]),
        .fills_done(f0), .gemms_done(g0), .drains_done(d0)
    );

    mx_cluster_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                    .MGR_X(1), .MGR_Y(2), .ACU_X(2), .ACU_Y(2),
                    .MEM_X(0), .MEM_Y(2),
                    .TILES(256), .GA(32), .GB(32), .MODEL(MODEL)) cu1 (
        .clk(clk), .resetn(rstn),
        .m_in_data(l_out[0][1]), .m_in_valid(l_outv[0][1]), .m_in_busy(l_outb[0][1]),
        .m_out_data(l_in[0][1]), .m_out_valid(l_inv[0][1]), .m_out_busy(l_inb[0][1]),
        .a_in_data(l_out[1][1]), .a_in_valid(l_outv[1][1]), .a_in_busy(l_outb[1][1]),
        .a_out_data(l_in[1][1]), .a_out_valid(l_inv[1][1]), .a_out_busy(l_inb[1][1]),
        .fills_done(f1), .gemms_done(g1), .drains_done(d1)
    );

    // ============================================================ AXI tasks
    task axi_w(input [31:0] a, input [63:0] d);
        begin
            @(posedge clk);
            awaddr <= a; awlen <= 0; awid <= 4'h1; awvalid <= 1'b1;
            @(posedge clk); while (!awready) @(posedge clk);
            awvalid <= 1'b0;
            wdata <= d; wlast <= 1'b1; wvalid <= 1'b1; bready <= 1'b1;
            @(posedge clk); while (!wready) @(posedge clk);
            wvalid <= 1'b0; wlast <= 1'b0;
            @(posedge clk); while (!bvalid) @(posedge clk);
            bready <= 1'b0;
        end
    endtask

    task axi_r(input [31:0] a, output [63:0] d);
        begin
            @(posedge clk);
            araddr <= a; arlen <= 0; arid <= 4'h2; arvalid <= 1'b1;
            @(posedge clk); while (!arready) @(posedge clk);
            arvalid <= 1'b0; rready <= 1'b1;
            @(posedge clk); while (!rvalid) @(posedge clk);
            d = rdata;
            @(posedge clk); rready <= 1'b0;
        end
    endtask

    task bd_write(input integer w, input [255:0] d);
        begin
            @(negedge clk);
            bd_we <= 1'b1; bd_addr <= w[15:0]; bd_wdata <= d;
            @(negedge clk);
            bd_we <= 1'b0;
        end
    endtask

    // ======================================================== the problem
    integer signed A [0:M-1][0:KK-1];
    integer signed B [0:KK-1][0:N-1];
    // scales are per (row, K block) -- microscaling shares a scale along the
    // reduction dimension only, so a K=128 problem has 4 scales per row
    integer        SA [0:M-1][0:NKB-1];
    integer        SB [0:N-1][0:NKB-1];
    integer        ANCHOR;

    real  fp64_c [0:M-1][0:N-1];
    integer signed exact_c [0:M-1][0:N-1];
    real  hw_c   [0:M-1][0:N-1];

    integer errors = 0, checks = 0;
    real    worst_rel = 0.0, sum_rel = 0.0;
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

    integer i, j, k, g, h, c, t, idx, seed;
    integer signed bsum;
    reg [255:0] wtmp, res_word;
    reg [63:0]  rv;
    reg [FW-1:0] instr;
    reg [33:0]  ad34;
    reg [7:0]   n8;
    real got_r, want_r, err;

    // stage one instruction flit at program slot `idx`
    task stage(input integer slot, input [FW-1:0] fl, input lastf);
        begin
            for (t = 0; t < WORDS; t = t + 1)
                axi_w(A_STAGE + (slot*WORDS + t)*8, fl[t*DW +: DW]);
        end
    endtask

    initial begin
        seed = 32'hBEEF1234;
        awvalid=0; wvalid=0; arvalid=0; bready=0; rready=0;
        awid=0; arid=0; awaddr=0; araddr=0; awlen=0; arlen=0;
        wdata=0; wlast=0; bd_we=0; bd_addr=0; bd_wdata=0;
        ANCHOR = 8;

        #200; repeat (10) @(posedge clk); rstn = 1; repeat (10) @(posedge clk);

        // ---------------------------------------------------------------
        $display("--- 1. C[%0d,%0d] = A[%0d,%0d] x B[%0d,%0d], split by output ---",
                 M, N, M, KK, KK, N);
        for (i = 0; i < M; i = i + 1)
            for (k = 0; k < KK; k = k + 1) A[i][k] = ($random(seed) & 127) - 64;
        for (k = 0; k < KK; k = k + 1)
            for (j = 0; j < N; j = j + 1) B[k][j] = ($random(seed) & 127) - 64;
        for (i = 0; i < M; i = i + 1)
            for (c = 0; c < NKB; c = c + 1) SA[i][c] = ($random(seed) & 3);
        for (j = 0; j < N; j = j + 1)
            for (c = 0; c < NKB; c = c + 1) SB[j][c] = ($random(seed) & 3);

        for (i = 0; i < M; i = i + 1)
            for (j = 0; j < N; j = j + 1) begin
                exact_c[i][j] = 0;
                fp64_c[i][j]  = 0.0;
                for (c = 0; c < NKB; c = c + 1) begin
                    bsum = 0;
                    for (k = 0; k < 32; k = k + 1)
                        bsum = bsum + A[i][c*32+k]*B[c*32+k][j];
                    exact_c[i][j] = exact_c[i][j] + (bsum <<< (SA[i][c] + SB[j][c]));
                    fp64_c[i][j]  = fp64_c[i][j]
                                  + $itor(bsum) * (2.0 ** (SA[i][c]+SB[j][c]-ANCHOR));
                end
                checks = checks + 1;
                if ($itor(exact_c[i][j]) * (2.0 ** -ANCHOR) != fp64_c[i][j]) begin
                    errors = errors + 1;
                    if (errors <= 4) $display("  FAIL models disagree [%0d][%0d]", i, j);
                end
            end
        $display("    %0d elements over K=%0d (%0d blocks), two ground truths agree",
                 M*N, KK, NKB);

        // ---------------------------------------------------------------
        $display("--- 2. preload operands ---");
        // L1 entry index is (group * NKB + kblock) -- K-outer sweep order --
        // and each entry is 4 consecutive words, one per K-slice.
        for (g = 0; g < GM; g = g + 1)
          for (idx = 0; idx < NKB; idx = idx + 1)
            for (c = 0; c < 4; c = c + 1) begin
                wtmp = 256'd0;
                for (i = 0; i < 4; i = i + 1)
                    for (k = 0; k < 8; k = k + 1)
                        wtmp[255 - (i*8+k)*7 -: 7] = A[g*4+i][idx*32 + c*8+k][6:0];
                for (i = 0; i < 4; i = i + 1)
                    wtmp[31 - i*8 -: 8] = SA[g*4+i][idx][7:0];
                bd_write(WA_BASE + (g*NKB + idx)*4 + c, wtmp);
            end
        for (h = 0; h < GNC*2; h = h + 1)
          for (idx = 0; idx < NKB; idx = idx + 1)
            for (c = 0; c < 4; c = c + 1) begin
                wtmp = 256'd0;
                for (k = 0; k < 8; k = k + 1)
                    for (j = 0; j < 4; j = j + 1)
                        wtmp[255 - (k*4+j)*7 -: 7] = B[idx*32 + c*8+k][h*4+j][6:0];
                for (j = 0; j < 4; j = j + 1)
                    wtmp[31 - j*8 -: 8] = SB[h*4+j][idx][7:0];
                if (h < GNC) bd_write(WB0_BASE + ((h)*NKB + idx)*4 + c, wtmp);
                else         bd_write(WB1_BASE + ((h-GNC)*NKB + idx)*4 + c, wtmp);
            end
        $display("    A %0d words, B %0d words", NEA*4, NEB*2*4);

        // ---------------------------------------------------------------
        // Programs are dispatched one cluster at a time: the orchestrator has
        // a single prog_dst, so two clusters means two kicks.
        $display("--- 3. cluster 0: FILL A, FILL B, GEMM, DRAIN ---");
        run_program(0, WA_BASE, WB0_BASE, WC0_BASE);
        $display("    cu0 fills=%0d gemms=%0d drains=%0d", f0, g0, d0);

        $display("--- 4. cluster 1 ---");
        run_program(1, WA_BASE, WB1_BASE, WC1_BASE);
        $display("    cu1 fills=%0d gemms=%0d drains=%0d", f1, g1, d1);

        $display("    memory: reads=%0d writes=%0d", mem_rd, mem_wr);

        // ---------------------------------------------------------------
        $display("--- 5. read back both halves ---");
        for (c = 0; c < 2; c = c + 1)
            for (t = 0; t < NT; t = t + 1) begin
                @(negedge clk);
                bd_addr <= ((c == 0) ? WC0_BASE : WC1_BASE) + t;
                @(negedge clk); @(negedge clk);
                res_word = bd_rdata;
                for (i = 0; i < 4; i = i + 1)
                    for (j = 0; j < 4; j = j + 1)
                        hw_c[(t / GNC)*4 + i][c*(N/2) + (t % GNC)*4 + j]
                            = fp16_to_real(res_word[(i*4+j)*16 +: 16]);
            end

        // ---------------------------------------------------------------
        $display("--- 6. precision over %0d elements ---", M*N);
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

        $display("");
        $display("    C[0][0]  hw %0f  fp64 %0f", hw_c[0][0], fp64_c[0][0]);
        $display("    C[0][32] hw %0f  fp64 %0f", hw_c[0][32], fp64_c[0][32]);
        $display("    worst rel err %0e   mean %0e   (FP16 ULP %0e)",
                 worst_rel, sum_rel / $itor(nz), 1.0/1024.0);
        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors  (MODEL=%0d)", checks, MODEL);
        else             $display("  FAIL -- %0d checks, %0d errors  (MODEL=%0d)", checks, errors, MODEL);
        $display("========================================");
        $finish;
    end

    // one cluster's whole program: fill A, fill B, sweep, drain
    task run_program(input integer cu, input integer wa, input integer wb,
                     input integer wc);
        integer s;
        begin
            s = 0;
            ad34 = wa * 32; n8 = NEA[7:0];
            instr = { 16'h0000, 4'h5, 8'h40, 1'b0, 3'b000,
                      4'd1, ad34, n8, 1'b0, 1'b0,
                      8'd0, 8'd0, 8'd0, 8'd0, 176'd0 };
            stage(s, instr, 1'b0); s = s + 1;

            ad34 = wb * 32; n8 = NEB[7:0];
            instr = { 16'h0000, 4'h5, 8'h40, 1'b0, 3'b000,
                      4'd1, ad34, n8, 1'b1, 1'b0,
                      8'd0, 8'd0, 8'd0, 8'd0, 176'd0 };
            stage(s, instr, 1'b0); s = s + 1;

            ad34 = 34'd0; n8 = 8'd0;
            instr = { 16'h0000, 4'h5, 8'h40, 1'b0, 3'b000,
                      4'd2, ad34, n8, 1'b0, 1'b0,
                      GM[7:0], GNC[7:0], NKB[7:0], ANCHOR[7:0], 176'd0 };
            stage(s, instr, 1'b0); s = s + 1;

            ad34 = wc * 32; n8 = NT[7:0];
            instr = { 16'h0000, 4'h5, 8'h40, 1'b1, 3'b000,
                      4'd3, ad34, n8, 1'b0, 1'b0,
                      8'd0, 8'd0, 8'd0, ANCHOR[7:0], 176'd0 };
            stage(s, instr, 1'b1); s = s + 1;

            // prog_dst is {Y, X}; managers live at (1,1) and (1,2)
            axi_w(A_PROG_DST,  {56'd0, 4'd1 + cu[3:0], 4'd1});
            axi_w(A_PROG_LEN,  s);
            axi_w(A_PROG_CRED, 64'd8);
            axi_w(A_PROG_KICK, 64'd1);

            t = 0;
            while (((cu == 0) ? d0 : d1) == 16'd0 && t < 40000) begin
                repeat (20) @(posedge clk); t = t + 1;
            end
            repeat (400) @(posedge clk);

            axi_r(32'h0058, rv);
            $display("    cu%0d PROG_STAT run=%0d left=%0d credit=%0d",
                     cu, rv[0], rv[16:1], rv[32:17]);
            axi_r(A_NODE + ({4'd1 + cu[3:0], 4'd1}) * 8, rv);
            $display("    cu%0d NODE_STATUS valid=%0d code=%02h signals=%0d",
                     cu, rv[0], rv[63:56], rv[23:8]);
        end
    endtask

    initial begin
        #400000000;
        $display("WATCHDOG -- f0=%0d g0=%0d d0=%0d f1=%0d g1=%0d d1=%0d rd=%0d wr=%0d",
                 f0, g0, d0, f1, g1, d1, mem_rd, mem_wr);
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire
