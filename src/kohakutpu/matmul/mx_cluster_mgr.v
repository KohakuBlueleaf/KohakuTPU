// Cluster manager: L1 and the GEMM sweep.
//
// One cluster is 4 tensor CUs in a DSP cascade plus an accumulator. The chain
// eats A[4][32] + B[32][4] every cycle -- eight 256-bit operand words -- and a
// NoC port delivers one. The gap is closed by REUSE, not bandwidth, which is
// why L1 exists and why the manager owns it explicitly.
//
// See docs/compute/tensor-isa.md. This module is the GEMM half; the FILL half
// (tensor descriptors -> memory requests -> L1) is mx_tdesc.v plus the fill
// engine above it.
//
//   L1A[g]  A rows 4g..4g+3, all 32 K   -- 896 bits + 32 bits of E8M0 scale
//   L1B[h]  B cols 4h..4h+3, all 32 K   -- likewise
//
// K IS THE OUTER LOOP. For each K block, sweep every output sub-tile:
//
//     for kb in 0..NK-1:  for g in 0..Gm-1:  for h in 0..Gn-1
//
// so a given accumulator address recurs only every Gm*Gn cycles instead of
// every cycle. That is what lets the accumulator be a plain memory with a
// synchronous read -- with K inner, back-to-back same-address accumulation
// cannot close a pipelined adder loop at all. It is a loop-order choice with
// an architectural consequence.
//
// THE ACU COMMAND RIDES A FIFO, NOT A MATCHED DELAY. The chain has ~19 cycles
// of latency and it is a function of NTCU and the skew SRLs. Rather than
// duplicate that constant here -- where it would rot the moment the chain
// changes -- each issue pushes {op, addr, scales} into a FIFO and every
// part_valid pops one. Order is preserved by construction, so alignment holds
// whatever the latency turns out to be.

`default_nettype none

module mx_cluster_mgr #(
    parameter integer GA     = 32,      // L1 A entries (row groups x K blocks)
    parameter integer GB     = 64,      // L1 B entries (col groups x K blocks)
    parameter integer TAW    = 9,       // accumulator tile address width
    parameter         L1_PRIM = "distributed"
)(
    input  wire         clk,
    input  wire         rst,

    // ---- backdoor L1 load (bench / fill engine) ------------------------
    input  wire         l1_we,
    input  wire         l1_sel,         // 0 = A, 1 = B
    input  wire [15:0]  l1_addr,
    input  wire [927:0] l1_data,        // {32'b scales, 896'b elements}

    // ---- GEMM command --------------------------------------------------
    input  wire         gemm_start,
    input  wire [7:0]   gemm_gm,        // row groups
    input  wire [7:0]   gemm_gn,        // column groups
    input  wire [7:0]   gemm_nk,        // K blocks
    input  wire [7:0]   gemm_anchor,
    output wire         gemm_busy,

    // ---- to the TCU chain ----------------------------------------------
    output reg  [895:0] a_out,
    output reg  [895:0] b_out,
    output reg          core_valid,
    output reg          core_first,

    // ---- from the TCU chain --------------------------------------------
    input  wire         part_valid,

    // ---- to the accumulator --------------------------------------------
    output wire [2:0]      acu_op,
    output wire [TAW-1:0]  acu_addr,
    output wire            acu_cmd,
    output wire [31:0]     acu_sa,
    output wire [31:0]     acu_sb,
    output wire [7:0]      acu_anchor
);
    localparam [2:0] OP_NOP = 3'd0, OP_LOAD = 3'd1, OP_ADD = 3'd2;

    localparam integer AAW = (GA <= 1) ? 1 : $clog2(GA);
    localparam integer BAW = (GB <= 1) ? 1 : $clog2(GB);

    // ================================================ L1
    // Explicit primitive, never inferred -- see CLAUDE.md. 928 bits wide and
    // ~tens deep is a distributed-RAM shape: the block/ultra ports are 72 bits,
    // so width would set the primitive count and depth would be 99% wasted.
    wire [927:0] a_ent, b_ent;
    reg  [AAW-1:0] a_rd;
    reg  [BAW-1:0] b_rd;

    kohaku_sdpram #(.WIDTH(928), .DEPTH(GA), .MEM_PRIM(L1_PRIM), .READ_LAT(1))
    u_l1a (.clk(clk),
           .wr_en(l1_we && !l1_sel), .wr_addr(l1_addr[AAW-1:0]), .wr_data(l1_data),
           .rd_en(1'b1), .rd_addr(a_rd), .rd_data(a_ent));

    kohaku_sdpram #(.WIDTH(928), .DEPTH(GB), .MEM_PRIM(L1_PRIM), .READ_LAT(1))
    u_l1b (.clk(clk),
           .wr_en(l1_we && l1_sel), .wr_addr(l1_addr[BAW-1:0]), .wr_data(l1_data),
           .rd_en(1'b1), .rd_addr(b_rd), .rd_data(b_ent));

    // ================================================ sweep counters
    reg [7:0] gm_r, gn_r, nk_r, anc_r;
    reg [7:0] kb, g, h;
    reg       run, s1_valid;
    reg       s1_first;
    reg [TAW-1:0] s1_addr;

    reg           s1b_valid, s1b_first;
    reg [TAW-1:0] s1b_addr;
    reg           s2_valid, s2_first;
    reg [TAW-1:0] s2_addr;
    reg [31:0]    s2_sa, s2_sb;

    wire          cmd_empty;      // driven by the ACU command FIFO below

    wire last_h  = (h + 8'd1 == gn_r);
    wire last_g  = last_h && (g + 8'd1 == gm_r);
    wire last_kb = last_g && (kb + 8'd1 == nk_r);

    // NOT just "the last tile has been issued". The cascade is ~19 cycles deep,
    // so when the counters finish there are still that many results in flight.
    // Reporting done there let DRAIN seize the accumulator control mux and cut
    // them off -- the tail sub-tiles came back as zeros.
    //
    // `cmd_empty` is the exact condition: every issued tile has a command
    // sitting in the FIFO until its own part_valid pops it, so an empty FIFO
    // means the chain has fully drained. No latency constant to keep in sync.
    assign gemm_busy = run || s1_valid || s1b_valid || s2_valid || !cmd_empty;

    // stage 0: counters present the L1 addresses; stage 1 consumes the data.
    // The RAM read is synchronous, so the address has to lead the use by one
    // cycle -- which is exactly what an explicit READ_LAT=1 makes visible.
    always @(posedge clk) begin
        if (rst) begin
            run <= 1'b0; s1_valid <= 1'b0;
            kb <= 8'd0; g <= 8'd0; h <= 8'd0;
            gm_r <= 8'd1; gn_r <= 8'd1; nk_r <= 8'd1; anc_r <= 8'd0;
            a_rd <= {AAW{1'b0}}; b_rd <= {BAW{1'b0}};
            s1_first <= 1'b0; s1_addr <= {TAW{1'b0}};
        end else begin
            s1_valid <= 1'b0;

            if (gemm_start && !run) begin
                run  <= 1'b1;
                gm_r <= (gemm_gm == 8'd0) ? 8'd1 : gemm_gm;
                gn_r <= (gemm_gn == 8'd0) ? 8'd1 : gemm_gn;
                nk_r <= (gemm_nk == 8'd0) ? 8'd1 : gemm_nk;
                anc_r <= gemm_anchor;
                kb <= 8'd0; g <= 8'd0; h <= 8'd0;
                a_rd <= {AAW{1'b0}};
                b_rd <= {BAW{1'b0}};
            end else if (run) begin
                // present addresses for (g,h,kb); the data lands next cycle
                a_rd <= (g * nk_r + kb);
                b_rd <= (h * nk_r + kb);
                s1_valid <= 1'b1;
                s1_first <= (kb == 8'd0);
                s1_addr  <= (g * gn_r + h);

                if (last_kb)     run <= 1'b0;
                if (last_h) begin
                    h <= 8'd0;
                    if (last_g) begin g <= 8'd0; kb <= kb + 8'd1; end
                    else        g <= g + 8'd1;
                end else h <= h + 8'd1;
            end
        end
    end

    // TWO cycles of control delay, not one. The counters assign a_rd at cycle
    // T; the RAM sees that address during T+1; with READ_LAT=1 the data is
    // valid during T+2. Consuming a_ent alongside s1_* (T+1) reads the
    // PREVIOUS entry, which shifts every result by one sub-tile -- structured,
    // silent, and it looked like an addressing bug rather than a timing one.
    //
    // This is the read latency being visible because the primitive is named.
    // Inferred LUTRAM with an asynchronous read would have hidden it here and
    // produced it again as a timing failure later.
    always @(posedge clk) begin
        if (rst) begin
            s1b_valid <= 1'b0; s1b_first <= 1'b0; s1b_addr <= {TAW{1'b0}};
            core_valid <= 1'b0; core_first <= 1'b0;
            a_out <= 896'd0; b_out <= 896'd0;
            s2_valid <= 1'b0; s2_first <= 1'b0; s2_addr <= {TAW{1'b0}};
            s2_sa <= 32'd0; s2_sb <= 32'd0;
        end else begin
            s1b_valid <= s1_valid;
            s1b_first <= s1_first;
            s1b_addr  <= s1_addr;

            core_valid <= s1b_valid;
            core_first <= s1b_first;
            a_out      <= a_ent[895:0];
            b_out      <= b_ent[895:0];

            s2_valid <= s1b_valid;
            s2_first <= s1b_first;
            s2_addr  <= s1b_addr;
            s2_sa    <= a_ent[927:896];
            s2_sb    <= b_ent[927:896];
        end
    end

    // ================================================ ACU command FIFO
    // Depth 64: the chain is ~19 deep and this must never fill, because a full
    // FIFO would silently drop a command and corrupt one output element.
    localparam integer CW = 3 + TAW + 64 + 8;

    wire [CW-1:0] cmd_in = { s2_first ? OP_LOAD : OP_ADD, s2_addr,
                             s2_sa, s2_sb, anc_r };
    wire [CW-1:0] cmd_out;
    wire          cmd_full;

    sync_fifo #(.DATA_WIDTH(CW), .FIFO_DEPTH(64), .MEMORY_TYPE("distributed"))
    u_cmd (.clk(clk), .rst(rst),
           .wr_en(s2_valid), .wr_data(cmd_in), .wr_busy(cmd_full),
           .rd_en(part_valid), .rd_data(cmd_out), .rd_busy(cmd_empty));

    assign acu_op     = part_valid ? cmd_out[CW-1 -: 3] : OP_NOP;
    assign acu_addr   = cmd_out[CW-4 -: TAW];
    assign acu_sa     = cmd_out[71:40];
    assign acu_sb     = cmd_out[39:8];
    assign acu_anchor = cmd_out[7:0];
    assign acu_cmd    = part_valid && !cmd_empty;

`ifndef SYNTHESIS
    always @(posedge clk) if (!rst && s2_valid && cmd_full)
        $display("%0t ERROR mx_cluster_mgr: ACU command FIFO overflow", $time);
    always @(posedge clk) if (!rst && part_valid && cmd_empty)
        $display("%0t ERROR mx_cluster_mgr: part_valid with no pending command", $time);
`endif

endmodule

`default_nettype wire
