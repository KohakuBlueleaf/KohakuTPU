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
//   L1A[g]  A rows 4g..4g+3, all 32 K   -- 896 bits + 4 x E5M3 scale
//   L1B[h]  B cols 4h..4h+3, all 32 K   -- likewise
//
// K IS THE OUTER LOOP. For each K block, sweep every output sub-tile:
//
//     for kb in 0..NK-1:  for g in 0..Gm-1:  for h in 0..Gn-1
//
// so an accumulator address recurs every Gm*Gn cycles instead of every cycle,
// which is what lets the accumulator be a plain memory with a synchronous read.
//
// THE ACU COMMAND RIDES A FIFO, NOT A MATCHED DELAY. The chain's ~19 cycles are
// a function of NTCU and the skew SRLs, so rather than duplicate that constant
// here each issue pushes {op, addr, scales} and every part_valid pops one.
// Order is preserved by construction, whatever the latency turns out to be.

`default_nettype none

// DOUBLE PUMPED. One L1 entry per clk2x cycle is two K blocks per clk cycle.
// Unpumped shipping file is mx_cluster_mgr.v, and this must track its sweep.
module mx_cluster_mgr_pump #(
    parameter integer GA     = 32,      // L1 A entries (row groups x K blocks)
    parameter integer GB     = 64,      // L1 B entries (col groups x K blocks)
    parameter integer TAW    = 9,       // accumulator tile address width
    parameter         L1_PRIM = "distributed"
)(
    input  wire         clk,
    input  wire         clk2x,
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
    input  wire         gemm_acc,   // add into the resident tile, do not reload
    // WHERE IN L1 THIS SWEEP'S OPERANDS ARE. They make L1 addressable, so the
    // driver can fill one region while another is swept and leave an unchanged
    // operand in place. See docs/isa/cluster.md s4.6.
    input  wire [7:0]   gemm_aoff,
    input  wire [7:0]   gemm_boff,
    // WHICH HALF of L1 this sweep reads. An L1 deeper than an 8-bit offset can
    // name is TWO BANKS of 256 rather than a wider offset field: the address is
    // {bank, offset} truncated to AAW, so at GA = 256 the bank bit falls off the
    // top and the address is byte-identical to what it was. Widening `aoff`
    // instead would have moved every field below it in the instruction.
    input  wire         gemm_abank,
    input  wire         gemm_bbank,
    // HAND EACH SUB-TILE OUT AS IT FINISHES. Set on the sweep that completes an
    // output tile: every issue of the LAST K block becomes OP_ADD_EMIT, so the
    // finished value leaves on the command that produced it rather than needing
    // a DRAIN pass with a command slot per sub-tile.
    input  wire         gemm_emit,
    // Backpressure from whatever collects those results. Holding the sweep is
    // the only place it can be applied: once a command is issued its result WILL
    // come out ~19 cycles later, so there is nothing downstream to stall.
    input  wire         emit_stall,
    output wire         emit_issue,     // one pulse per sub-tile handed out
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
    // Fix A needs BOTH phases' scales: one E5M3 pair per K block, and a pumped
    // cycle reads two blocks.
    output wire [31:0]     acu_sa2,
    output wire [31:0]     acu_sb2,
    // The pair's second K block is past NK: an odd NK ends on a lone block.
    output wire            acu_single,
    output wire [7:0]      acu_anchor
);
    localparam [2:0] OP_NOP = 3'd0, OP_LOAD = 3'd1, OP_ADD = 3'd2,
                     OP_ADD_EMIT = 3'd7;

    localparam integer AAW = (GA <= 1) ? 1 : $clog2(GA);
    localparam integer BAW = (GB <= 1) ? 1 : $clog2(GB);

    // ================================================ L1
    // Explicit primitive, never inferred -- see CLAUDE.md. 928 bits wide and
    // ~tens deep is a distributed-RAM shape: the block/ultra ports are 72 bits,
    // so width would set the primitive count and depth would be 99% wasted.
    wire [927:0] a_ent, b_ent;
    reg  [AAW-1:0] a_rd;
    reg  [BAW-1:0] b_rd;

    // PHASE. `tog` flips once per clk cycle; sampled in clk2x it differs from
    // its own delayed copy for exactly the SECOND 2x cycle of each 1x cycle.
    reg tog, tog_q, tog_qq;
    always @(posedge clk) begin
        if (rst) tog <= 1'b0;
        else     tog <= ~tog;
    end
    always @(posedge clk2x) begin
        if (rst) begin tog_q <= 1'b0; tog_qq <= 1'b0; end
        else     begin tog_q <= tog;  tog_qq <= tog_q; end
    end
    // kb FIRST then kb+1: the reverse order put the out-of-range entry of an odd
    // NK into p_lo, which is exactly the half `single` keeps.
    wire ph1 = tog_q ^ tog_qq;

    // +1 WRAPS INSIDE THE BANK, as a_off8 does: across the full width, offset
    // 255 carries into the bank bit and the pair's half reads the other buffer.
    wire [AAW-1:0] a_rd2 = (AAW > 8) ? {a_rd[AAW-1:8], a_rd[7:0] + {7'd0, ph1}}
                                     : a_rd + {{(AAW-1){1'b0}}, ph1};
    wire [BAW-1:0] b_rd2 = (BAW > 8) ? {b_rd[BAW-1:8], b_rd[7:0] + {7'd0, ph1}}
                                     : b_rd + {{(BAW-1){1'b0}}, ph1};

    // Phase of the DATA, one 2x cycle behind the address that fetched it.
    reg        ph1_d;
    reg        s1v_2x, s1f_2x;
    reg [31:0] sa_p0, sb_p0, sa_p1, sb_p1;

    // The write port is on clk2x too: two clocks into one RAM would need a true
    // dual-port primitive, and a fill is never concurrent with a sweep's read.
    kohaku_sdpram #(.WIDTH(928), .DEPTH(GA), .MEM_PRIM(L1_PRIM), .READ_LAT(1))
    u_l1a (.clk(clk2x),
           .wr_en(l1_we && !l1_sel), .wr_addr(l1_addr[AAW-1:0]), .wr_data(l1_data),
           .rd_en(1'b1), .rd_addr(a_rd2), .rd_data(a_ent));

    kohaku_sdpram #(.WIDTH(928), .DEPTH(GB), .MEM_PRIM(L1_PRIM), .READ_LAT(1))
    u_l1b (.clk(clk2x),
           .wr_en(l1_we && l1_sel), .wr_addr(l1_addr[BAW-1:0]), .wr_data(l1_data),
           .rd_en(1'b1), .rd_addr(b_rd2), .rd_data(b_ent));

    // ================================================ sweep counters
    reg [7:0] gm_r, gn_r, nk_r, anc_r;
    reg [7:0] aoff_r, boff_r;
    reg       abank_r, bbank_r;
    reg       acc_r, emit_r;
    reg [7:0] kb, g, h;
    reg       run, s1_valid;
    reg       s1_first, s1_emit;
    reg [TAW-1:0] s1_addr;

    reg           s1_single, s1b_single, s2_single;
    reg           s1b_valid, s1b_first, s1b_emit;
    reg [TAW-1:0] s1b_addr;
    reg           s2_valid, s2_first, s2_emit;
    reg [TAW-1:0] s2_addr;
    reg [31:0]    s2_sa, s2_sb, s2_sa2, s2_sb2;

    wire          cmd_empty;      // driven by the ACU command FIFO below

    // REUSE PACING. An address recurs every Gm*Gn issues, and the accumulator
    // needs REUSE_MIN cycles between two commands to one address (mx_acu_fp.v).
    // A smaller tiling recurs faster than the accumulator can turn around and
    // accumulates onto a stale tile -- silently, and only for part of the sweep.
    // Idle cycles restore the gap; at Gm*Gn >= REUSE_MIN this is zero.
    localparam integer ACU_REUSE_MIN = 5;
    reg [2:0] pace, pace_n;

    // NOTHING HERE NEEDS THE PRODUCT Gm*Gn -- only which of five buckets it
    // lands in. As a multiply it is an 8x8 fabric multiplier whose 16-bit result
    // feeds three comparators, and it closed the whole CU:
    //
    //   gn_r -> Gm*Gn -> compare 1 / 2 / >=5 -> pace_n     15 levels
    //
    // Both operands are at least 1, so the buckets are small-value tests on the
    // operands themselves. See docs/compute/accumulator.md s4.
    wire [7:0] gm_w = (gemm_gm == 8'd0) ? 8'd1 : gemm_gm;
    wire [7:0] gn_w = (gemm_gn == 8'd0) ? 8'd1 : gemm_gn;

    wire t_is1   = (gm_w == 8'd1) && (gn_w == 8'd1);
    wire t_is2   = ((gm_w == 8'd1) && (gn_w == 8'd2))
                || ((gm_w == 8'd2) && (gn_w == 8'd1));
    // Gm*Gn < 5 with both operands >= 1 is exactly these three shapes:
    // (1,<=4), (<=4,1) and (2,2).
    wire t_small = ((gm_w == 8'd1) && (gn_w <= 8'd4))
                || ((gn_w == 8'd1) && (gm_w <= 8'd4))
                || ((gm_w == 8'd2) && (gn_w == 8'd2));

`ifndef SYNTHESIS
    // Those three shapes are the expansion of `Gm*Gn < 5` and do NOT follow
    // ACU_REUSE_MIN if it moves. Pacing a short tiling wrong corrupts part of a
    // K sweep and nothing else, so no bench would necessarily fail.
    initial if (ACU_REUSE_MIN != 5)
        $display("ERROR mx_cluster_mgr_pump: pace buckets are expanded for ACU_REUSE_MIN=5, got %0d",
                 ACU_REUSE_MIN);
`endif

    // The offset stays 8 bits and CANNOT carry into the bank: `aoff + gm*nk`
    // beyond 256 wraps inside its own half, which is the same silent wrap the
    // capacity note in docs/isa/cluster.md s4.7 already describes. Widening the
    // sum here would instead walk a sweep into the other bank's operands.
    wire [7:0] a_off8 = aoff_r + g * nk_r + kb;
    wire [7:0] b_off8 = boff_r + h * nk_r + kb;
    wire [8:0] a_lin  = {abank_r, a_off8};
    wire [8:0] b_lin  = {bbank_r, b_off8};
    wire [8:0] a_st   = {gemm_abank, gemm_aoff};
    wire [8:0] b_st   = {gemm_bbank, gemm_boff};

`ifndef SYNTHESIS
    // One bank bit is two banks. A deeper L1 needs a second, and the truncation
    // below would silently drop the top address bit rather than fail.
    initial if ((AAW > 9) || (BAW > 9))
        $display("ERROR mx_cluster_mgr_pump: L1 is two banks of 256, so GA and GB cap at 512 (AAW=%0d BAW=%0d)",
                 AAW, BAW);
`endif

    // Two K blocks per issue, so the counter steps by two and the last-block
    // test has to match it or the sweep re-reads what it just summed.
    localparam [7:0] KSTEP = 8'd2;

    wire last_h  = (h + 8'd1 == gn_r);
    wire last_g  = last_h && (g + 8'd1 == gm_r);
    wire on_last_kb = (kb + KSTEP >= nk_r);  // every issue of the final K block
    wire last_kb = last_g && on_last_kb;

    // NOT just "the last tile has been issued": the cascade is ~19 cycles deep,
    // so when the counters finish that many results are still in flight, and
    // DRAIN seizing the control mux there discards them (they come back zero).
    //
    // NOT `!cmd_empty` either. That is the FIFO's registered flag and it
    // deasserts two cycles after a push, leaving a hole where this reads idle
    // with commands still queued. Only a tiling short enough to finish inside
    // the hole hits it -- every bench down to Gm*Gn = 3 passed, a 2 sub-tile one
    // did not. Counting issued-minus-retired is exact and owes nothing to FIFO
    // timing or to any latency constant.
    reg [7:0] pending;
    always @(posedge clk) begin
        if (rst) pending <= 8'd0;
        else     pending <= pending + (s2_valid  ? 8'd1 : 8'd0)
                                    - (part_valid ? 8'd1 : 8'd0);
    end

    assign gemm_busy = run || s1_valid || s1b_valid || s2_valid || (pending != 8'd0);

    // stage 0: counters present the L1 addresses; stage 1 consumes the data.
    // The RAM read is synchronous, so the address has to lead the use by one
    // cycle -- which is exactly what an explicit READ_LAT=1 makes visible.
    always @(posedge clk) begin
        if (rst) begin
            run <= 1'b0; s1_valid <= 1'b0;
            kb <= 8'd0; g <= 8'd0; h <= 8'd0;
            gm_r <= 8'd1; gn_r <= 8'd1; nk_r <= 8'd1; anc_r <= 8'd0;
            aoff_r <= 8'd0; boff_r <= 8'd0;
            abank_r <= 1'b0; bbank_r <= 1'b0;
            acc_r <= 1'b0; emit_r <= 1'b0;
            // a_rd/b_rd/s1_addr are addresses `s1_valid` qualifies downstream.
            s1_first <= 1'b0; s1_emit <= 1'b0;
            pace <= 3'd0; pace_n <= 3'd0;
        end else begin
            s1_valid <= 1'b0;

            if (gemm_start && !run) begin
                run  <= 1'b1;
                gm_r <= (gemm_gm == 8'd0) ? 8'd1 : gemm_gm;
                gn_r <= (gemm_gn == 8'd0) ? 8'd1 : gemm_gn;
                nk_r <= (gemm_nk == 8'd0) ? 8'd1 : gemm_nk;
                anc_r <= gemm_anchor;
                acc_r <= gemm_acc;
                emit_r <= gemm_emit;
                aoff_r <= gemm_aoff;
                boff_r <= gemm_boff;
                abank_r <= gemm_abank;
                bbank_r <= gemm_bbank;
                kb <= 8'd0; g <= 8'd0; h <= 8'd0;
                a_rd <= a_st[AAW-1:0];
                b_rd <= b_st[BAW-1:0];
                // one K block never revisits an address, so it never paces
                pace_n <= (gemm_nk <= 8'd1 || !t_small) ? 3'd0
                        : t_is1 ? 3'd4
                        : t_is2 ? 3'd2 : 3'd1;
                pace <= 3'd0;
            end else if (run) begin
                // Held, not slowed: the counters do not move, so the only cost
                // is the cycle.
                if (emit_stall) begin
                    // nothing -- wait for the collector to catch up
                end else if (pace != 3'd0) pace <= pace - 3'd1;
                else begin
                    // present addresses for (g,h,kb); the data lands next cycle
                    a_rd <= a_lin[AAW-1:0];
                    b_rd <= b_lin[BAW-1:0];
                    s1_valid <= 1'b1;
                    // First K block of a NON-accumulating GEMM loads the tile;
                    // everything else adds to it. `acc_r` lets one output tile
                    // be built by several instructions -- what a K longer than
                    // L1 requires -- without spilling the partial to memory.
                    s1_first <= (kb == 8'd0) && !acc_r;
                    // The last K block completes every sub-tile it touches, so
                    // every issue in it -- not only the final one -- carries
                    // the value out.
                    s1_emit  <= emit_r && on_last_kb;
                    s1_single <= (kb + 8'd1 >= nk_r);
                    s1_addr  <= (g * gn_r + h);
                    pace     <= pace_n;

                    if (last_kb)     run <= 1'b0;
                    if (last_h) begin
                        h <= 8'd0;
                        if (last_g) begin g <= 8'd0; kb <= kb + KSTEP; end
                        else        g <= g + 8'd1;
                    end else h <= h + 8'd1;
                end
            end
        end
    end

    // TWO cycles of control delay, not one. The counters assign a_rd at cycle T;
    // the RAM sees that address during T+1; with READ_LAT=1 the data is valid
    // during T+2. Consuming a_ent alongside s1_* (T+1) reads the PREVIOUS entry,
    // which shifts every result by one sub-tile -- structured and silent, and it
    // reads as an addressing bug rather than a timing one.
    always @(posedge clk) begin
        // VALIDS ONLY: the addresses and the scale pair are gated by them.
        if (rst) begin
            s1b_valid <= 1'b0; s1b_first <= 1'b0;
            s1b_emit <= 1'b0; s2_emit <= 1'b0;
            s2_valid <= 1'b0; s2_first <= 1'b0;
        end else begin
            s1b_valid <= s1_valid;
            s1b_first <= s1_first;
            s1b_emit  <= s1_emit;
            s1b_addr  <= s1_addr;
            s1b_single <= s1_single;
            s2_single  <= s1b_single;

            s2_valid <= s1b_valid;
            s2_first <= s1b_first;
            s2_emit  <= s1b_emit;
            s2_addr  <= s1b_addr;
            // sa pairs with part_in (the SECOND phase), sa2 with part_in2.
            // ONE SAMPLE POINT: sa_p0 taken earlier raced its own clk2x capture.
            s2_sa    <= sa_p1;
            s2_sb    <= sb_p1;
            s2_sa2   <= sa_p0;
            s2_sb2   <= sb_p0;
        end
    end

    always @(posedge clk2x) begin
        ph1_d <= ph1;
        if (!ph1_d) begin
            sa_p0 <= a_ent[927:896]; sb_p0 <= b_ent[927:896];
        end else begin
            sa_p1 <= a_ent[927:896]; sb_p1 <= b_ent[927:896];
        end
    end

    // ================================================ operand register, 2x
    // L1 clock-to-out lands here; nothing else has to close at the 2x period.
    always @(posedge clk2x) begin
        // a_out/b_out alone are 1,792 flops that `core_valid` qualifies, and a
        // reset that wide is what makes routing impossible.
        if (rst) begin
            core_valid <= 1'b0; core_first <= 1'b0;
        end else begin
            // TWO clk2x registers off s1_valid: a_out holds kb in clk2x cycle 2
            // and kb+1 in cycle 3, so s1b_valid is late and s1_valid is early.
            s1v_2x     <= s1_valid;
            s1f_2x     <= s1_first;
            core_valid <= s1v_2x;
            core_first <= s1f_2x;
            a_out      <= a_ent[895:0];
            b_out      <= b_ent[895:0];
        end
    end

    // ================================================ ACU command FIFO
    // Depth 64: the chain is ~19 deep and this must never fill, because a full
    // FIFO would silently drop a command and corrupt one output element.
    // 128 bits of scale, not 64: fix A carries a pair per K block and a pumped
    // cycle has two.
    localparam integer CW = 3 + TAW + 128 + 8 + 1;

    // OP_LOAD_EMIT does not exist and does not need to: an emitting sweep is the
    // LAST chunk of an output tile, so `s2_first` and `s2_emit` cannot both be
    // set. kernel.plan guarantees it and falls back to a DRAIN when it cannot.
    wire [CW-1:0] cmd_in = { s2_first ? OP_LOAD :
                             s2_emit  ? OP_ADD_EMIT : OP_ADD, s2_addr,
                             s2_sa, s2_sb, s2_sa2, s2_sb2, s2_single, anc_r };
    assign emit_issue = s2_valid && s2_emit;
    wire [CW-1:0] cmd_out;
    wire          cmd_full;

    sync_fifo #(.DATA_WIDTH(CW), .FIFO_DEPTH(64), .MEMORY_TYPE("distributed"))
    u_cmd (.clk(clk), .rst(rst),
           .wr_en(s2_valid), .wr_data(cmd_in), .wr_busy(cmd_full),
           .rd_en(part_valid), .rd_data(cmd_out), .rd_busy(cmd_empty));

    assign acu_op     = part_valid ? cmd_out[CW-1 -: 3] : OP_NOP;
    assign acu_addr   = cmd_out[CW-4 -: TAW];
    assign acu_sa     = cmd_out[136:105];
    assign acu_sb     = cmd_out[104:73];
    assign acu_sa2    = cmd_out[72:41];
    assign acu_sb2    = cmd_out[40:9];
    assign acu_single = cmd_out[8];
    assign acu_anchor = cmd_out[7:0];
    assign acu_cmd    = part_valid && !cmd_empty;

`ifndef SYNTHESIS
    // A FILL may run while a sweep does -- the point of `aoff`/`boff` -- which
    // makes L1 a shared resource with no interlock. A fill landing on entries
    // the sweep is reading corrupts a few sub-tiles and nothing else: the median
    // barely moves and the answer is wrong. The driver owns the banking.
    // WITHIN A BANK. `l1_addr` is {bank, offset} and a fill into the OTHER half
    // is the whole point of banking, so comparing the flat address would report
    // every double-buffered fill as a collision -- and a check that cries wolf
    // is deleted, which is how the real one gets lost.
    wire [7:0] fill_off  = l1_addr[7:0];
    wire       fill_bank = (AAW > 8) ? l1_addr[8] : 1'b0;
    wire       fill_bnkb = (BAW > 8) ? l1_addr[8] : 1'b0;
    always @(posedge clk) if (!rst && gemm_busy && l1_we) begin
        if (!l1_sel && (fill_bank == abank_r) &&
                       ({8'd0, fill_off} >= {8'd0, aoff_r}) &&
                       ({8'd0, fill_off} <  {8'd0, aoff_r} + gm_r * nk_r))
            $display("%0t ERROR mx_cluster_mgr_pump: FILL A entry %0d of bank %0d lands inside the running sweep [%0d,%0d)",
                     $time, fill_off, fill_bank, aoff_r, aoff_r + gm_r * nk_r);
        if (l1_sel && (fill_bnkb == bbank_r) &&
                      ({8'd0, fill_off} >= {8'd0, boff_r}) &&
                      ({8'd0, fill_off} <  {8'd0, boff_r} + gn_r * nk_r))
            $display("%0t ERROR mx_cluster_mgr_pump: FILL B entry %0d of bank %0d lands inside the running sweep [%0d,%0d)",
                     $time, fill_off, fill_bnkb, boff_r, boff_r + gn_r * nk_r);
    end
    always @(posedge clk) if (!rst && s2_valid && cmd_full)
        $display("%0t ERROR mx_cluster_mgr_pump: ACU command FIFO overflow", $time);
    always @(posedge clk) if (!rst && part_valid && cmd_empty)
        $display("%0t ERROR mx_cluster_mgr_pump: part_valid with no pending command", $time);
    // The LOAD wins, so the sub-tile is computed and never handed out, and the
    // drain waits for results that are not coming.
    always @(posedge clk) if (!rst && s2_valid && s2_first && s2_emit)
        $display("%0t ERROR mx_cluster_mgr_pump: emitting sweep also opens tile %0d",
                 $time, s2_addr);
`endif

endmodule

`default_nettype wire
