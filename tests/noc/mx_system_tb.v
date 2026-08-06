// End-to-end: a real matmul on a 1x5 NoC.
//
//                        [ matmul CU (1,0) ]
//                                 |  north
//     west --  +----------------------------------+  -- east -- [ fake mem (2,1) ]
//              |          router (1,1)            |
//              +----------------------------------+
//                                 |  local
//                        [ orchestrator (1,1) ]
//                                 |  AXI4
//                              (host)
//
// The whole path, with nothing stubbed except DRAM itself:
//
//   host stages a program over AXI  ->  orchestrator dispatches CU_INST
//   -> CU issues MEM_RD_REQ naming ITSELF as src
//   -> memory replies MEM_RD_RESP straight to the CU, not via the orchestrator
//   -> cluster computes 4x32x4 per block, ACU accumulates in FP24
//   -> CU issues MEM_WR_REQ with the FP16 result
//   -> CU signals completion, orchestrator mirrors it into NODE_STATUS
//   -> host polls AXI and reads the result back
//
// C[4,4] = A[4,256] * B[256,4], as 8 blocks of K=32, with per-block per-row and
// per-column E8M0 scales -- so this exercises microscaling, not just a matmul.
//
// PRECISION. Three quantities are compared:
//
//   EXACT INT  the int7 matmul as a CPU would compute it, in integer arithmetic
//              with the block scales applied by shifting. This is what the
//              hardware datapath must reproduce with no error at all.
//   FP64       the same sum in `real`. Identical to EXACT INT here by
//              construction -- the check that they agree is what proves the
//              bench's own model has not drifted.
//   HARDWARE   the FP16 written back to memory.
//
// So the reported error is purely what the FP24 accumulator and the FP16
// emission cost. Quantisation error is NOT included: the operands are already
// int7 in memory, which is the point at which MAS would have quantised them.

`default_nettype none
`timescale 1ns/1ps

// Behavioural DSP by default; compile tests/matmul/mx_model_dsp.v ahead of this
// to run against the real DSP48E2 (needs -L unisims_ver and glbl).
`ifndef MX_MODEL
`define MX_MODEL 1
`endif

module mx_system_tb;

    localparam integer FW    = 288;
    localparam integer PW    = 4;
    localparam integer DW    = 64;
    localparam integer WORDS = 5;              // 64-bit words per flit
    localparam integer NBLK  = 8;              // K = 8 * 32 = 256
    localparam integer MODEL = `MX_MODEL;

    // node coordinates
    localparam integer ORC_X = 1, ORC_Y = 1;
    localparam integer CU_X  = 1, CU_Y  = 0;
    localparam integer MEM_X = 2, MEM_Y = 1;

    // orchestrator register map
    localparam [31:0] A_PROG_DST = 32'h0040, A_PROG_LEN  = 32'h0048,
                      A_PROG_KICK= 32'h0050, A_PROG_STAT = 32'h0058,
                      A_PROG_CRED= 32'h0060, A_NODE      = 32'h1000,
                      A_STAGE    = 32'h2000;

    // memory word layout (256-bit words)
    localparam integer WA_BASE = 0;            // A: 4 words per block
    localparam integer WB_BASE = 64;           // B: 4 words per block
    localparam integer WC_BASE = 200;          // result

    reg clk = 0, rstn = 0;
    always #2 clk = ~clk;

    // ------------------------------------------------------------ AXI wires
    reg  [3:0]  awid, arid;
    reg  [31:0] awaddr, araddr;
    reg  [7:0]  awlen, arlen;
    reg         awvalid, arvalid, wvalid, wlast, bready, rready;
    reg  [63:0] wdata;
    wire        awready, wready, bvalid, arready, rvalid, rlast;
    wire [63:0] rdata;
    wire [3:0]  bid, rid;
    wire [1:0]  bresp, rresp;

    // ---------------------------------------------------- router <-> nodes
    wire [FW-1:0] r_l_out, r_n_out, r_e_out;
    wire          r_l_outv, r_n_outv, r_e_outv;
    wire          r_l_outb, r_n_outb, r_e_outb;
    wire [FW-1:0] l_r_in, n_r_in, e_r_in;
    wire          l_r_inv, n_r_inv, e_r_inv;
    wire          l_r_inb, n_r_inb, e_r_inb;

    NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("distributed"),
                .POS_WIDTH(PW), .POS_X(1), .POS_Y(1),
                .GRID_LO(1), .GRID_HI(1)) rtr (
        .clk(clk), .rst(!rstn),
        .local_in_data(l_r_in), .local_in_valid(l_r_inv), .local_in_busy(l_r_inb),
        .local_out_data(r_l_out), .local_out_valid(r_l_outv), .local_out_busy(r_l_outb),
        .north_in_data(n_r_in), .north_in_valid(n_r_inv), .north_in_busy(n_r_inb),
        .north_out_data(r_n_out), .north_out_valid(r_n_outv), .north_out_busy(r_n_outb),
        .east_in_data(e_r_in), .east_in_valid(e_r_inv), .east_in_busy(e_r_inb),
        .east_out_data(r_e_out), .east_out_valid(r_e_outv), .east_out_busy(r_e_outb),
        // south and west are unused on a 1x1 grid; nothing ever routes there
        .south_in_data({FW{1'b0}}), .south_in_valid(1'b0), .south_in_busy(),
        .south_out_data(), .south_out_valid(), .south_out_busy(1'b0),
        .west_in_data({FW{1'b0}}), .west_in_valid(1'b0), .west_in_busy(),
        .west_out_data(), .west_out_valid(), .west_out_busy(1'b0)
    );

    // ------------------------------------------------------- orchestrator
    noc_orchestrator #(.DATA_WIDTH(DW), .ADDR_WIDTH(32), .ID_WIDTH(4),
                       .FLIT_WIDTH(FW), .POS_WIDTH(PW),
                       .GRID_LO(1), .GRID_HI(1),
                       .ORC_X(ORC_X), .ORC_Y(ORC_Y), .STAGE_FLITS(32)) orc (
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
        .noc_out_data(l_r_in), .noc_out_valid(l_r_inv), .noc_out_busy(l_r_inb),
        .noc_in_data(r_l_out), .noc_in_valid(r_l_outv), .noc_in_busy(r_l_outb)
    );

    // ---------------------------------------------------------- matmul CU
    wire [15:0] blocks_done, emits_done;

    mx_matmul_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                   .POS_X(CU_X), .POS_Y(CU_Y),
                   .MEM_X(MEM_X), .MEM_Y(MEM_Y),
                   .TILES(16), .TAW(4), .MODEL(MODEL)) cu (
        .clk(clk), .resetn(rstn),
        .noc_in_data(r_n_out), .noc_in_valid(r_n_outv), .noc_in_busy(r_n_outb),
        .noc_out_data(n_r_in), .noc_out_valid(n_r_inv), .noc_out_busy(n_r_inb),
        .blocks_done(blocks_done), .emits_done(emits_done)
    );

    // -------------------------------------------------------- fake memory
    reg          bd_we;
    reg  [15:0]  bd_addr;
    reg  [255:0] bd_wdata;
    wire [255:0] bd_rdata;
    wire [15:0]  mem_rd, mem_wr;

    noc_fake_mem #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                   .POS_X(MEM_X), .POS_Y(MEM_Y), .WORDS(1024)) fmem (
        .clk(clk), .resetn(rstn),
        .noc_in_data(r_e_out), .noc_in_valid(r_e_outv), .noc_in_busy(r_e_outb),
        .noc_out_data(e_r_in), .noc_out_valid(e_r_inv), .noc_out_busy(e_r_inb),
        .bd_we(bd_we), .bd_addr(bd_addr), .bd_wdata(bd_wdata), .bd_rdata(bd_rdata),
        .rd_count(mem_rd), .wr_count(mem_wr)
    );

    // ============================================================ AXI tasks
    // AW and W are taken in sequence by the orchestrator's write FSM -- awready
    // and wready are never both high, so waiting on the pair deadlocks. Same
    // shape as noc_orchestrator_tb, which is already proven against this slave.
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

    // ======================================================== the problem
    integer signed A [0:3][0:255];
    integer signed B [0:255][0:3];
    integer        SA [0:NBLK-1][0:3];      // E8M0 per block, per row of A
    integer        SB [0:NBLK-1][0:3];      // E8M0 per block, per column of B
    integer        ANCHOR;

    integer signed exact_c [0:3][0:3];      // CPU int7 ground truth
    real           fp64_c  [0:3][0:3];      // FP64 ground truth

    integer errors = 0, checks = 0;
    real    worst_rel = 0.0;

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

    task bd_write(input integer w, input [255:0] d);
        begin
            @(negedge clk);
            bd_we <= 1'b1; bd_addr <= w[15:0]; bd_wdata <= d;
            @(negedge clk);
            bd_we <= 1'b0;
        end
    endtask

    integer i, j, k, b, c, t, seed, sh;
    integer signed blocksum;
    reg [255:0] wtmp;
    reg [63:0]  rv;
    reg [FW-1:0] instr;
    reg [255:0] res_word;
    reg [33:0]  adr_a34, adr_b34;
    real got_r, want_r, err;

    initial begin
        seed = 32'hC0FFEE01;
        awvalid = 0; wvalid = 0; arvalid = 0; bready = 0; rready = 0;
        awid = 0; arid = 0; awaddr = 0; araddr = 0; awlen = 0; arlen = 0;
        wdata = 0; wlast = 0; bd_we = 0; bd_addr = 0; bd_wdata = 0;
        ANCHOR = 6;

        #200;
        repeat (10) @(posedge clk);
        rstn = 1;
        repeat (10) @(posedge clk);

        // ---------------------------------------------------------------
        $display("--- 1. build the problem and both ground truths ---");
        for (i = 0; i < 4; i = i + 1)
            for (k = 0; k < 256; k = k + 1) A[i][k] = ($random(seed) & 127) - 64;
        for (k = 0; k < 256; k = k + 1)
            for (j = 0; j < 4; j = j + 1) B[k][j] = ($random(seed) & 127) - 64;
        for (b = 0; b < NBLK; b = b + 1)
            for (i = 0; i < 4; i = i + 1) begin
                SA[b][i] = ($random(seed) & 3);
                SB[b][i] = ($random(seed) & 3);
            end

        for (i = 0; i < 4; i = i + 1)
            for (j = 0; j < 4; j = j + 1) begin
                exact_c[i][j] = 0;
                fp64_c[i][j]  = 0.0;
            end
        for (b = 0; b < NBLK; b = b + 1)
            for (i = 0; i < 4; i = i + 1)
                for (j = 0; j < 4; j = j + 1) begin
                    blocksum = 0;
                    for (k = 0; k < 32; k = k + 1)
                        blocksum = blocksum + A[i][b*32+k] * B[b*32+k][j];
                    sh = SA[b][i] + SB[b][j] - ANCHOR;
                    // exact: shifts here are negative, so track it in FP64 and
                    // keep the integer form scaled by 2^ANCHOR to stay exact
                    exact_c[i][j] = exact_c[i][j]
                                  + (blocksum <<< (SA[b][i] + SB[b][j]));
                    fp64_c[i][j]  = fp64_c[i][j] + $itor(blocksum) * (2.0 ** sh);
                end
        // the two models must agree, or the bench is measuring itself
        for (i = 0; i < 4; i = i + 1)
            for (j = 0; j < 4; j = j + 1) begin
                checks = checks + 1;
                if ($itor(exact_c[i][j]) * (2.0 ** -ANCHOR) != fp64_c[i][j]) begin
                    errors = errors + 1;
                    $display("  FAIL ground-truth models disagree at [%0d][%0d]", i, j);
                end
            end

        // ---------------------------------------------------------------
        $display("--- 2. preload operands into memory ---");
        for (b = 0; b < NBLK; b = b + 1) begin
            for (c = 0; c < 4; c = c + 1) begin
                wtmp = 256'd0;
                for (i = 0; i < 4; i = i + 1)
                    for (k = 0; k < 8; k = k + 1)
                        wtmp[255 - (i*8+k)*7 -: 7] = A[i][b*32 + c*8 + k][6:0];
                for (i = 0; i < 4; i = i + 1)
                    wtmp[31 - i*8 -: 8] = SA[b][i][7:0];
                bd_write(WA_BASE + b*4 + c, wtmp);
            end
            for (c = 0; c < 4; c = c + 1) begin
                wtmp = 256'd0;
                for (k = 0; k < 8; k = k + 1)
                    for (j = 0; j < 4; j = j + 1)
                        wtmp[255 - (k*4+j)*7 -: 7] = B[b*32 + c*8 + k][j][6:0];
                for (j = 0; j < 4; j = j + 1)
                    wtmp[31 - j*8 -: 8] = SB[b][j][7:0];
                bd_write(WB_BASE + b*4 + c, wtmp);
            end
        end

        // ---------------------------------------------------------------
        // NBLK BLOCK instructions then one EMIT, staged as CU_INST flits.
        // The dispatcher rewrites dst and src, so only type/txn/last and the
        // payload are staged here.
        $display("--- 3. stage the program ---");
        // Addresses go through explicitly-sized regs. Writing the expression
        // straight into the concatenation makes it contribute 32 bits, not the
        // 34 the field is, so the payload silently comes out 4 bits short and
        // every field below it shifts. See docs/simulation.md s3.
        for (b = 0; b < NBLK; b = b + 1) begin
            adr_a34 = (WA_BASE + b*4) * 32;
            adr_b34 = (WB_BASE + b*4) * 32;
            instr = { 16'h0000, 4'h5, 8'h40, 1'b0, 3'b000,
                      4'd1,                                  // opcode BLOCK
                      adr_a34,                               // A byte address
                      adr_b34,                               // B byte address
                      4'd0,                                  // tile 0
                      (b == 0),                              // first
                      ANCHOR[7:0],
                      171'd0 };
            for (t = 0; t < WORDS; t = t + 1)
                axi_w(A_STAGE + (b*WORDS + t)*8, instr[t*DW +: DW]);
        end
        adr_a34 = WC_BASE * 32;
        adr_b34 = 34'd0;
        instr = { 16'h0000, 4'h5, 8'h40, 1'b1, 3'b000,
                  4'd2,                                      // opcode EMIT
                  adr_a34,                                   // destination
                  adr_b34, 4'd0, 1'b0, ANCHOR[7:0], 171'd0 };
        for (t = 0; t < WORDS; t = t + 1)
            axi_w(A_STAGE + (NBLK*WORDS + t)*8, instr[t*DW +: DW]);

        // ---------------------------------------------------------------
        $display("--- 4. dispatch and wait ---");
        axi_w(A_PROG_DST,  {56'd0, CU_Y[3:0], CU_X[3:0]});
        axi_w(A_PROG_LEN,  64'd9);           // NBLK + 1
        axi_w(A_PROG_CRED, 64'd16);
        axi_w(A_PROG_KICK, 64'd1);

        t = 0;
        while (emits_done == 16'd0 && t < 4000) begin
            repeat (20) @(posedge clk);
            t = t + 1;
        end
        repeat (200) @(posedge clk);

        $display("    blocks=%0d emits=%0d  mem reads=%0d writes=%0d",
                 blocks_done, emits_done, mem_rd, mem_wr);
        checks = checks + 1;
        if (blocks_done != NBLK) begin
            errors = errors + 1;
            $display("  FAIL only %0d of %0d blocks executed", blocks_done, NBLK);
        end
        checks = checks + 1;
        if (emits_done != 1) begin
            errors = errors + 1;
            $display("  FAIL emit did not complete");
        end

        // ---------------------------------------------------------------
        $display("--- 5. orchestrator saw the completion ---");
        axi_r(A_NODE + ({CU_Y[3:0], CU_X[3:0]}) * 8, rv);
        checks = checks + 1;
        if (rv[0] !== 1'b1) begin
            errors = errors + 1;
            $display("  FAIL NODE_STATUS.valid not set for the CU");
        end
        $display("    NODE_STATUS: code=%02h signals=%0d", rv[63:56], rv[23:8]);

        // ---------------------------------------------------------------
        $display("--- 6. precision: hardware vs FP64 and CPU int7 ---");
        @(negedge clk);
        bd_addr <= WC_BASE[15:0];
        @(negedge clk); @(negedge clk);
        res_word = bd_rdata;

        for (i = 0; i < 4; i = i + 1)
            for (j = 0; j < 4; j = j + 1) begin
                got_r  = fp16_to_real(res_word[(i*4+j)*16 +: 16]);
                want_r = fp64_c[i][j];
                checks = checks + 1;
                if (want_r == 0.0) begin
                    if (got_r != 0.0) errors = errors + 1;
                end else begin
                    err = (got_r - want_r) / want_r;
                    if (err < 0.0) err = -err;
                    if (err > worst_rel) worst_rel = err;
                    // FP16 output carries 11 significand bits; allow a few ULP
                    // for the FP24 accumulation across 8 blocks on top
                    if (err > 4.0 / 1024.0) begin
                        errors = errors + 1;
                        if (errors <= 8)
                            $display("  FAIL C[%0d][%0d] got %0f want %0f relerr %0e",
                                     i, j, got_r, want_r, err);
                    end
                end
            end

        $display("");
        $display("    C[0][0]  hardware %0f   fp64 %0f   exact int %0d / 2^%0d",
                 fp16_to_real(res_word[0 +: 16]), fp64_c[0][0], exact_c[0][0], ANCHOR);
        $display("    worst relative error vs FP64: %0e   (FP16 ULP = %0e)",
                 worst_rel, 1.0/1024.0);

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors  (MODEL=%0d)", checks, MODEL);
        else             $display("  FAIL -- %0d checks, %0d errors  (MODEL=%0d)", checks, errors, MODEL);
        $display("========================================");
        $finish;
    end

    initial begin
        #20000000;
        $display("WATCHDOG TIMEOUT -- blocks=%0d emits=%0d rd=%0d wr=%0d",
                 blocks_done, emits_done, mem_rd, mem_wr);
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire
