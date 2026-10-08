// The vector core as a NoC endpoint, driven by a SIMULATED NoC STREAM.
//
// No mesh and no orchestrator: the bench drives noc_in_*/noc_out_* directly,
// the way tests/noc/cu_base_tb.v does, and also plays MEMORY -- it answers
// MEM_RD_REQ and absorbs MEM_WR_REQ/MEM_WR_DATA. That is the whole point of
// the exercise: the core has to be wireable without being wired.
//
// The kernel is a full round trip through every layer that exists:
//
//   VFILL  DRAM -> L1        two 128-element FP16 arrays, 16 words
//   VBAR   wait for the fill to retire
//   VLD    L1 -> v0, v1      converting FP16 -> E8M15 on the way in
//   VADD   v2 = v0 + v1      16 lanes, FLAT
//   VST    v2 -> L1          converting E8M15 -> FP16 on the way out
//   VDRAIN L1 -> DRAM
//
// Operands are small integers, so every expected value is an equality.

`default_nettype none
`timescale 1ns/1ps

// -d TB_RFPAD=36 / -d TB_RFPACK=2: the register file's padded / packed shapes.
`ifndef TB_RFPAD
`define TB_RFPAD 24
`endif
`ifndef TB_RFPACK
`define TB_RFPACK 1
`endif
// -d TB_L1ULTRA: L1 in URAM, read latency 2, so every walk's landing moves a cycle.
`ifdef TB_L1ULTRA
`define TB_L1PRIM "ultra"
`define TB_KD_BOUND 460
`else
`define TB_L1PRIM "block"
`define TB_KD_BOUND 380
`endif

module vec_cu_tb;
    localparam FW = 288;
    localparam PW = 4;
    localparam CX = 3, CY = 3;      // the vector CU
    localparam HX = 0, HY = 0;      // us, the agent
    localparam MX = 1, MY = 1;      // us again, as memory

    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_RD_RESP = 4'h2, T_MEM_WR_DATA = 4'h4;
    localparam [3:0] T_CU_INST = 4'h5, T_CU_SIGNAL = 4'h6, T_CU_DATA = 4'h8;
    localparam [7:0] SIG_DATA_RECEIVED = 8'h03, SIG_FAULT = 8'h04;

    localparam A_SRC = 34'h1000, A_DST = 34'h2000, A_DST2 = 34'h3000;
    localparam A_DST3 = 34'h4000;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg  [FW-1:0] in_data;
    reg           in_valid;
    wire          in_busy;
    wire [FW-1:0] out_data;
    wire          out_valid;
    reg           out_busy;
    wire [31:0]   dbg_cycles;
    wire          dbg_fault;

    vec_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(CX), .POS_Y(CY),
             .MEM_X(MX), .MEM_Y(MY), .INST_DEPTH(32), .MODEL(1),
             .L1_DEPTH(512), .L1_PRIM(`TB_L1PRIM),
             .RF_PAD(`TB_RFPAD), .RF_PACK(`TB_RFPACK)) dut (
        .clk(clk), .resetn(resetn),
        .noc_in_data(in_data), .noc_in_valid(in_valid), .noc_in_busy(in_busy),
        .noc_out_data(out_data), .noc_out_valid(out_valid),
        .noc_out_busy(out_busy),
        .dbg_cycles(dbg_cycles), .dbg_fault(dbg_fault)
    );

    integer errors = 0, checks = 0;

    task chk(input [63:0] got, input [63:0] want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                if (errors < 20) begin
                    $display("  FAIL %0s: got %0h want %0h", what, got, want);
                end
            end
        end
    endtask

    // FP16 for a small non-negative integer. Exact below 2048.
    function [15:0] f16i(input integer v);
        integer t, ex;
        reg [31:0] sh;
        begin
            if (v == 0) begin
                f16i = 16'd0;
            end
            else begin
                t = v; ex = 0;
                while (t > 1) begin t = t >> 1; ex = ex + 1; end
                if (ex <= 10) begin
                    sh = v << (10 - ex);
                end
                else begin
                    sh = v >> (ex - 10);
                end
                f16i = {1'b0, (5'd15 + ex[4:0]), sh[9:0]};
            end
        end
    endfunction

    // ================================================ memory model
    // Word addressed: one 256-bit line per 32 bytes. Reads are queued and
    // answered after a delay, so the core cannot depend on a fixed latency.
    reg [255:0] dram [0:1023];
    reg [39:0]  rq_addr [0:63];
    reg [7:0]   rq_tag  [0:63];
    reg [7:0]   rq_cnt  [0:63];   // words of a STREAM run (ew = 1 assumed), 1 for a plain read
    reg [7:0]   rq_sub;
    integer     rq_head, rq_tail, rq_wait;
    reg [39:0]  wr_addr_l;
    reg         wr_open;
    reg [8:0]   wr_left;
    // Bursts and their words, and any burst that is malformed: over 8 beats,
    // across 256 bytes, or data with no burst open.
    integer     n_wburst, n_wword, n_wbad;
    // Link backpressure: `out_busy` 1 cycle in 4 while set.
    reg         bp_on;
    always @(negedge clk) begin
        out_busy <= bp_on && (($random & 3) == 0);
    end
    // The memory's MEM_WR_ACK, one per write burst, sent between read responses.
    localparam [3:0] T_MEM_WR_ACK = 4'h3;
    integer     ack_owed;

    reg  [FW-1:0] mem_flit;
    reg           mem_valid;

    wire [3:0] o_type = out_data[FW-4*PW-1 -: 4];
    wire [7:0] o_txn  = out_data[FW-4*PW-5 -: 8];
    // 40, NOT 34. NOC_MEM_ADDR is [255:216]; decoding the top 34 reads the
    // address shifted right by six and the drain lands where nothing looks.
    wire [39:0] o_addr = out_data[255 -: 40];

    wire o_take = out_valid && !out_busy;
    wire wd_now = o_take && (o_type == T_MEM_WR_DATA) && wr_open && (wr_left == 9'd1);
    wire ack_go = (ack_owed > 0) && (rq_head == rq_tail) && !in_busy;

    integer sig_count, dr_count;
    reg [31:0] last_sig_arg, last_dr_arg;
    reg        last_sig_fault;
    reg [3:0]  last_sig_dx, last_sig_dy;
    wire [7:0] o_sig = out_data[255 -: 8];

    // The descriptor of the burst the DUT is currently sending, tracked by
    // counting its data flits rather than by position in the stream.
    reg [255:0] last_cud_desc;
    reg [8:0]   cud_left;

    // The mesh, for one node: CU_DATA the core emits is handed straight back to
    // it. A peer drain aimed at (CX,CY) is therefore an L1 -> NoC -> L1 copy,
    // and both halves of this ISA change are exercised against each other.
    reg [FW-1:0] lb_q [0:63];
    integer      lb_head, lb_tail, cud_out;
    reg [FW-1:0] lb_flit;
    reg          lb_valid;

    always @(posedge clk) begin
        if (!resetn) begin
            rq_head <= 0; rq_tail <= 0; rq_wait <= 0; ack_owed <= 0; rq_sub <= 8'd0;
            wr_open <= 1'b0; mem_valid <= 1'b0; wr_left <= 9'd0;
            n_wburst <= 0; n_wword <= 0; n_wbad <= 0;
            sig_count <= 0; last_sig_arg <= 32'd0; last_sig_fault <= 1'b0;
            dr_count <= 0; last_dr_arg <= 32'd0;
            lb_head <= 0; lb_tail <= 0; cud_out <= 0; lb_valid <= 1'b0;
            last_sig_dx <= 4'd0; last_sig_dy <= 4'd0;
            last_cud_desc <= 256'd0; cud_left <= 9'd0;
        end else begin
            mem_valid <= 1'b0;
            lb_valid  <= 1'b0;

            if (o_take) begin
                case (o_type)
                    T_MEM_RD_REQ: begin
                        rq_addr[rq_tail[5:0]] <= o_addr;
                        rq_tag[rq_tail[5:0]]  <= o_txn;
                        rq_cnt[rq_tail[5:0]]  <= (out_data[206] && (out_data[199 -: 8] != 8'd0))
                                                 ? out_data[199 -: 8] : 8'd1;
                        rq_tail <= rq_tail + 1;
                    end
                    T_MEM_WR_REQ: begin
                        wr_addr_l <= o_addr;
                        wr_open   <= 1'b1;
                        wr_left   <= {1'b0, out_data[215 -: 8]} + 9'd1;
                        n_wburst  <= n_wburst + 1;
                        if (wr_open || (out_data[215 -: 8] > 8'd7)
                            || ({1'b0, o_addr[7:5]} + out_data[215 -: 8] > 9'd7)) begin
                            n_wbad <= n_wbad + 1;
                        end
                    end
                    T_MEM_WR_DATA: if (wr_open) begin
                        dram[wr_addr_l[14:5]] <= out_data[255:0];
                        wr_addr_l <= wr_addr_l + 40'd32;
                        wr_left   <= wr_left - 9'd1;
                        n_wword   <= n_wword + 1;
                        if (wr_left == 9'd1) begin
                            wr_open <= 1'b0;
                        end
                    end else begin
                        n_wbad <= n_wbad + 1;
                    end
                    T_CU_SIGNAL: begin
                        sig_count      <= sig_count + 1;
                        last_sig_arg   <= out_data[247 -: 32];
                        last_sig_fault <= (o_sig == SIG_FAULT);
                        last_sig_dx    <= out_data[287 -: 4];
                        last_sig_dy    <= out_data[283 -: 4];
                        if (o_sig == SIG_DATA_RECEIVED) begin
                            dr_count    <= dr_count + 1;
                            last_dr_arg <= out_data[247 -: 32];
                        end
                    end
                    T_CU_DATA: begin
                        lb_q[lb_tail[5:0]] <= out_data;
                        lb_tail <= lb_tail + 1;
                        cud_out <= cud_out + 1;
                        if (cud_left == 9'd0) begin
                            last_cud_desc <= out_data[255:0];
                            cud_left <= {1'b0, out_data[231 -: 8]} + 9'd1;
                        end
                        else begin
                            cud_left <= cud_left - 9'd1;
                        end
                    end
                    default: ;
                endcase
            end

            // Only while nothing else is driving the link, and never into a
            // full receive queue: a masked flit would look like a lost one.
            if ((lb_head != lb_tail) && (rq_head == rq_tail) && !in_busy) begin
                lb_flit  <= lb_q[lb_head[5:0]];
                lb_valid <= 1'b1;
                lb_head  <= lb_head + 1;
            end

            // answer one queued word every 4 cycles; acknowledge writes between
            if (rq_head != rq_tail) begin
                if (rq_wait < 3) begin
                    rq_wait <= rq_wait + 1;
                end
                else begin
                    rq_wait  <= 0;
                    mem_flit <= { CX[3:0], CY[3:0], MX[3:0], MY[3:0],
                                  T_MEM_RD_RESP, rq_tag[rq_head[5:0]] + rq_sub,
                                  1'b1, 3'b000,
                                  dram[rq_addr[rq_head[5:0]][14:5] + {2'd0, rq_sub}] };
                    mem_valid <= 1'b1;
                    if (rq_sub + 8'd1 == rq_cnt[rq_head[5:0]]) begin
                        rq_head <= rq_head + 1;
                        rq_sub  <= 8'd0;
                    end else begin
                        rq_sub  <= rq_sub + 8'd1;
                    end
                end
            end
            else if (ack_go) begin
                mem_flit <= { CX[3:0], CY[3:0], MX[3:0], MY[3:0],
                              T_MEM_WR_ACK, 8'h00, 1'b1, 3'b000, 256'd0 };
                mem_valid <= 1'b1;
            end
            ack_owed <= ack_owed + (wd_now ? 1 : 0) - (ack_go ? 1 : 0);
        end
    end

    // ================================================ NoC stream into the CU
    // The agent's instruction stream and the memory's responses share the one
    // inbound link, exactly as they would through a router.
    reg  [FW-1:0] agent_flit;
    reg           agent_valid;

    always @(*) begin
        in_valid = 1'b0;
        in_data  = {FW{1'b0}};
        if (mem_valid) begin
            in_valid = 1'b1;
            in_data  = mem_flit;
        end else if (lb_valid) begin
            in_valid = 1'b1;
            in_data  = lb_flit;
        end else if (agent_valid) begin
            in_valid = 1'b1;
            in_data  = agent_flit;
        end
    end

    task send_cu(input [255:0] payload);
        begin
            @(negedge clk);
            while (in_busy || mem_valid || lb_valid) begin
                @(negedge clk);
            end
            agent_flit  = { CX[3:0], CY[3:0], HX[3:0], HY[3:0], T_CU_INST,
                            8'h20, 1'b0, 3'b000, payload };
            agent_valid = 1'b1;
            @(negedge clk);
            agent_valid = 1'b0;
        end
    endtask

    task put_imem(input [8:0] a, input [31:0] w);
        begin send_cu({4'd1, a, 211'd0, w}); end
    endtask

    task put_desc(input [2:0] ad, input [2:0] fld, input [33:0] v);
        begin send_cu({4'd2, ad, fld, v, 212'd0}); end
    endtask

    task do_run(input [8:0] p);
        begin send_cu({4'd3, p, 243'd0}); end
    endtask

    // A peer's CU_DATA. Same link as the instruction stream, so this is the
    // path a cluster on the mesh would take.
    task send_data_from(input [3:0] sx, input [3:0] sy,
                        input [255:0] payload, input lst);
        begin
            @(negedge clk);
            while (in_busy || mem_valid || lb_valid) begin
                @(negedge clk);
            end
            agent_flit  = { CX[3:0], CY[3:0], sx, sy, T_CU_DATA,
                            8'h00, lst, 3'b000, payload };
            agent_valid = 1'b1;
            @(negedge clk);
            agent_valid = 1'b0;
        end
    endtask

    task send_data(input [255:0] payload, input lst);
        begin send_data_from(HX[3:0], HY[3:0], payload, lst); end
    endtask

    task cud_desc(input [7:0] buf_id, input [15:0] off, input [7:0] len,
                  input sig);
        begin cud_desc_ack(buf_id, off, len, sig, 8'd0); end
    endtask

    task cud_desc_ack(input [7:0] buf_id, input [15:0] off, input [7:0] len,
                      input sig, input [7:0] ack);
        begin send_data({buf_id, off, len, 7'd0, sig, ack, 208'd0}, 1'b0); end
    endtask

    // ================================================ program
    localparam [31:0] I_VSETI    = 32'hD0000000;
    localparam [31:0] I_VSETVL   = 32'hC0000000;
    localparam [31:0] I_VSETMD   = 32'hC8000000;
    localparam [31:0] I_VFILL    = 32'hE8000000;
    localparam [31:0] I_VBAR     = 32'hE0000000;
    localparam [31:0] I_VLD_A1   = 32'hA1200000;
    localparam [31:0] I_VLD_A2   = 32'hA1420000;
    localparam [31:0] I_VADD     = 32'h18040220;
    localparam [31:0] I_VST_A3   = 32'hA9640000;
    // Drains ALL of L1: the two operand arrays and the sums, so a corrupted
    // stage is identifiable instead of merely visible.
    localparam [31:0] I_VDRAIN   = 32'hF0800000;
    localparam [31:0] I_VHALT    = 32'hF8000000;

    // Kernel 2, at pc 20: a D4 chain, then a TREE reduction broadcast back
    // into a vector. v3 = ((v0*1)+1)*1+1 = v0+2, and v4 = sum(v0) everywhere.
    localparam [31:0] I_MD_D4    = 32'hC8004000;
    localparam [31:0] I_MD_TREE  = 32'hC8006000;
    localparam [31:0] I_CH_MUL0  = 32'h29E60200;
    localparam [31:0] I_CH_ADD1  = 32'h1DE60220;
    localparam [31:0] I_CH_MUL2  = 32'h2DE60200;
    localparam [31:0] I_VST_A5   = 32'hA9A60000;
    localparam [31:0] I_VRED_SUM = 32'h98020000;
    localparam [31:0] I_VBCAST   = 32'hB2082000;
    localparam [31:0] I_VST_A6   = 32'hA9C80000;
    localparam [31:0] I_VDRAIN2  = 32'hF0E00018;

    // Kernel 3, at pc 40: VRED ANY/ALL. The point is the VL MASK, so a uniform
    // predicate would prove nothing -- an all-ones mask gives the same answer.
    // v0 holds 1..128, so comparing at 64 splits the predicate at element 64,
    // and reducing THAT under vl=64 is wrong in both directions if the mask is
    // stuck at 128: ANY(v0>64) is 0 and would read 1, ALL(v0<65) is 1 and would
    // read 0. Compares run at vl=128 so all 128 predicate bits are written.
    localparam [31:0] I_K_SET     = 32'hD6000000;   // VSETI with sa=SRC_K -> K3
    localparam [31:0] I_CMPGT_P0  = 32'h61E00660;   // v0 > K3 -> P0
    localparam [31:0] I_CMPLT_P1  = 32'h59E00668;   // v0 < K3 -> P1
    localparam [31:0] I_ANY_P0    = 32'h980600C0;   // VRED.ANY P0 -> S3
    localparam [31:0] I_ALL_P1    = 32'h980800E8;   // VRED.ALL P1 -> S4
    localparam [31:0] I_BC_S3_V8  = 32'hB2106000;
    localparam [31:0] I_BC_S4_V9  = 32'hB2128000;
    localparam [31:0] I_VST_V8_A5 = 32'hA9B00000;
    localparam [31:0] I_VST_V9_A6 = 32'hA9D20000;

    // Kernel 4, at pc 80: drain the two L1 words a peer wrote straight back out
    // to DRAM. VDRAIN A0 from L1 word 64.
    localparam [31:0] I_VDRAIN_CD = 32'hF0000040;

    // Kernel 5, at pc 90: drain L1 words 64..65 to the node at (3,3) -- us --
    // buffer 0, asking to be told when it lands. to_node = ir[24], signal =
    // ir[25], dst = ir[20:13], buf = ir[12:9]; A1 supplies the peer's L1 offset
    // in its base and the count in its bound. Kernel 6 at pc 92 drains where
    // that landed back out to DRAM, which is the only way to see it.
    localparam [31:0] I_VDRAIN_ND   = 32'hF3266040;
    localparam [31:0] I_VDRAIN_BACK = 32'hF00000C8;

    integer i, j, w, spin, s0;

    // Section 12's DRAM pattern: every line distinct, so a misplaced word is visible.
    function [255:0] pat(input integer k);
        pat = {16{k[15:0] + 16'h1000}};
    endfunction

    // Section 13: FP32 integers, exact through E8M15; line i holds 8i+1 .. 8i+8.
    function [31:0] f32i(input integer n);
        integer p;
        reg [31:0] m;
        begin
            p = 0;
            while ((n >> (p + 1)) != 0) p = p + 1;
            m = n << (23 - p);
            f32i = (n == 0) ? 32'd0 : {1'b0, 8'd127 + p[7:0], m[22:0]};
        end
    endfunction
    function [255:0] f32line(input integer i);
        integer e;
        begin
            for (e = 0; e < 8; e = e + 1) f32line[e*32 +: 32] = f32i(i*8 + e + 1);
        end
    endfunction
    // What section 13's kernels leave in L1 word w, given section 12 left pat(512+w).
    function [255:0] l1x(input integer w);
        begin
            if (w < 16) l1x = f32line(w);
            else if ((w >= 100) && (w <= 130) && ((w % 2) == 0)) l1x = f32line((w - 100) / 2);
            else l1x = pat(512 + w);
        end
    endfunction

    task fill_chk(input integer l1w, input integer dw, input [255:0] what);
        begin
            if (dram[l1w] !== pat(dw)) begin
                $display("    L1 %0d: got %0h want line %0d", l1w, dram[l1w][15:0], dw);
            end
            chk({63'd0, dram[l1w] === pat(dw)}, 64'd1, what);
        end
    endtask
    reg [255:0] line;
    reg [255:0] cud_line [0:1];
    reg [15:0]  got16, want16;

    initial begin
        agent_valid = 0; agent_flit = 0; bp_on = 0;
        for (i = 0; i < 1024; i = i + 1) begin
            dram[i] = 256'd0;
        end

        // a[i] = i+1 at 0x1000, b[i] = 2(i+1) at 0x1000 + 8 lines
        for (w = 0; w < 8; w = w + 1) begin
            line = 256'd0;
            for (i = 0; i < 16; i = i + 1) begin
                line[i*16 +: 16] = f16i(w*16 + i + 1);
            end
            dram[(A_SRC >> 5) + w] = line;
            line = 256'd0;
            for (i = 0; i < 16; i = i + 1) begin
                line[i*16 +: 16] = f16i(2*(w*16 + i + 1));
            end
            dram[(A_SRC >> 5) + 8 + w] = line;
        end

        repeat (8) @(negedge clk);
        resetn = 1;
        repeat (4) @(negedge clk);

        $display("--- 1. staging the kernel over the NoC ---");
        put_imem(9'd0,  I_VSETI);
        put_imem(9'd1,  32'd128);
        put_imem(9'd2,  I_VSETVL);
        put_imem(9'd3,  I_VSETMD);
        put_imem(9'd4,  I_VFILL);
        put_imem(9'd5,  I_VBAR);
        put_imem(9'd6,  I_VLD_A1);
        put_imem(9'd7,  I_VLD_A2);
        put_imem(9'd8,  I_VADD);
        put_imem(9'd9,  I_VST_A3);
        put_imem(9'd10, I_VDRAIN);
        put_imem(9'd11, I_VHALT);

        // A0 walks DRAM lines for the fill; A1..A3 walk L1 words; A4 the drain.
        put_desc(3'd0, 3'd0, A_SRC);
        put_desc(3'd0, 3'd1, {18'd32, 16'd16});
        put_desc(3'd1, 3'd0, 34'd0);
        put_desc(3'd1, 3'd1, {18'd1, 16'd8});
        put_desc(3'd2, 3'd0, 34'd8);
        put_desc(3'd2, 3'd1, {18'd1, 16'd8});
        put_desc(3'd3, 3'd0, 34'd16);
        put_desc(3'd3, 3'd1, {18'd1, 16'd8});
        put_desc(3'd4, 3'd0, A_DST);
        put_desc(3'd4, 3'd1, {18'd32, 16'd24});

        put_imem(9'd20, I_MD_D4);
        put_imem(9'd21, I_CH_MUL0);
        put_imem(9'd22, I_CH_ADD1);
        put_imem(9'd23, I_CH_MUL2);
        put_imem(9'd24, I_CH_ADD1);
        put_imem(9'd25, I_VSETMD);
        put_imem(9'd26, I_VST_A5);
        put_imem(9'd27, I_MD_TREE);
        put_imem(9'd28, I_VRED_SUM);
        put_imem(9'd29, I_VSETMD);
        put_imem(9'd30, I_VBCAST);
        put_imem(9'd31, I_VST_A6);
        put_imem(9'd32, I_VDRAIN2);
        put_imem(9'd33, I_VHALT);

        put_desc(3'd5, 3'd0, 34'd24);
        put_desc(3'd5, 3'd1, {18'd1, 16'd8});
        put_desc(3'd6, 3'd0, 34'd32);
        put_desc(3'd6, 3'd1, {18'd1, 16'd8});
        put_desc(3'd7, 3'd0, A_DST2);
        put_desc(3'd7, 3'd1, {18'd32, 16'd16});

        $display("--- 2. running it ---");
        do_run(9'd0);

        spin = 0;
        // 26 imem writes + 16 descriptor writes + 1 run
        while ((sig_count < 43) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 43, "one signal per CU instruction");
        chk({31'd0, dbg_fault}, 64'd0, "kernel must not fault");
        chk({31'd0, last_sig_fault}, 64'd0, "RUN must not report SIG_FAULT");
        if (spin >= 60000) begin
            $display("  FAIL kernel never retired");
        end
        $display("    kernel retired in %0d cycles", last_sig_arg);

        $display("--- 3. what landed in memory ---");
        for (w = 0; w < 24; w = w + 1) begin
            line = dram[(A_DST >> 5) + w];
            for (i = 0; i < 16; i = i + 1) begin
                got16 = line[i*16 +: 16];
                if (w < 8) begin
                    want16 = f16i(w*16 + i + 1);
                end
                else if (w < 16) begin
                    want16 = f16i(2*((w-8)*16 + i + 1));
                end
                else begin
                    want16 = f16i(3*((w-16)*16 + i + 1));
                end
                if (got16 !== want16) begin
                    $display("    word %0d elem %0d: got %04h want %04h",
                             w, i, got16, want16);
                end
                chk({48'd0, got16}, {48'd0, want16}, "drained word");
            end
        end

        $display("--- 4. second kernel: D4 chain and a TREE reduction ---");
        do_run(9'd20);
        spin = 0;
        while ((sig_count < 44) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 44, "second kernel retired");
        chk({31'd0, dbg_fault}, 64'd0, "second kernel must not fault");
        if (spin >= 60000) begin
            $display("  FAIL second kernel never retired");
        end
        $display("    second kernel retired in %0d cycles", last_sig_arg);

        for (w = 0; w < 16; w = w + 1) begin
            line = dram[(A_DST2 >> 5) + w];
            for (i = 0; i < 16; i = i + 1) begin
                got16 = line[i*16 +: 16];
                // words 0..7 are the D4 chain, 8..15 the broadcast sum
                want16 = (w < 8) ? f16i(w*16 + i + 3) : f16i(128*129/2);
                if (got16 !== want16) begin
                    $display("    k2 word %0d elem %0d: got %04h want %04h",
                             w, i, got16, want16);
                end
                chk({48'd0, got16}, {48'd0, want16}, "kernel 2 word");
            end
        end

        $display("--- 5. VRED ANY/ALL under a narrowed VL ---");
        // Staged after kernel 2 so its signal counts are untouched. I_VST_A5
        // appears twice as a BARRIER: a compare's predicate lands 14 cycles
        // behind issue, and the load/store ops are the ones that wait for
        // pipe_empty. Its L1 words are scratch and get overwritten below.
        // Both compares first, then ANY P0 and ALL P1: each reduction names a
        // predicate the instruction before it did not.
        put_imem(9'd40, I_VSETMD);
        put_imem(9'd41, I_VSETI);      put_imem(9'd42, 32'd128);
        put_imem(9'd43, I_VSETVL);
        put_imem(9'd44, I_K_SET);      put_imem(9'd45, 32'h00428000);   // 64.0
        put_imem(9'd46, I_CMPGT_P0);
        put_imem(9'd47, I_VST_A5);
        put_imem(9'd48, I_K_SET);      put_imem(9'd49, 32'h00428200);   // 65.0
        put_imem(9'd50, I_CMPLT_P1);
        put_imem(9'd51, I_VST_A5);
        put_imem(9'd52, I_VSETI);      put_imem(9'd53, 32'd64);
        put_imem(9'd54, I_VSETVL);
        put_imem(9'd55, I_ANY_P0);
        put_imem(9'd56, I_ALL_P1);
        put_imem(9'd57, I_BC_S3_V8);
        put_imem(9'd58, I_VST_V8_A5);
        put_imem(9'd59, I_BC_S4_V9);
        put_imem(9'd60, I_VST_V9_A6);
        put_imem(9'd61, I_VDRAIN2);
        put_imem(9'd62, I_VHALT);

        do_run(9'd40);
        spin = 0;
        // 23 imem writes on top of 44, then the kernel's own HALT
        while ((sig_count < 68) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 68, "third kernel retired");
        chk({31'd0, dbg_fault}, 64'd0, "third kernel must not fault");
        if (spin >= 60000) begin
            $display("  FAIL third kernel never retired");
        end

        // vl=64 writes 4 chunks, so only the first four words of each store
        // are the result; the rest is the barrier's scratch.
        for (w = 0; w < 4; w = w + 1) begin
            line = dram[(A_DST2 >> 5) + w];
            for (i = 0; i < 16; i = i + 1) begin
                chk({48'd0, line[i*16 +: 16]}, {48'd0, f16i(0)},
                    "ANY(v0>64) over vl=64 must be false");
            end
            line = dram[(A_DST2 >> 5) + 8 + w];
            for (i = 0; i < 16; i = i + 1) begin
                chk({48'd0, line[i*16 +: 16]}, {48'd0, f16i(1)},
                    "ALL(v0<65) over vl=64 must be true");
            end
        end

        $display("--- 6. a peer writes L1 directly: CU_DATA in ---");
        // Nothing here goes near memory: the words arrive over the NoC, land in
        // L1 at the descriptor's offset, and a VDRAIN reads them back out. If
        // the burst were counted as a fill retirement the drain would still
        // pass and the NEXT VBAR would hang, so the kernel below ends on one.
        put_imem(9'd80, I_VDRAIN_CD);
        put_imem(9'd81, I_VBAR);
        put_imem(9'd82, I_VHALT);
        put_desc(3'd0, 3'd0, A_DST3);
        put_desc(3'd0, 3'd1, {18'd32, 16'd2});

        for (w = 0; w < 2; w = w + 1) begin
            cud_line[w] = 256'd0;
            for (i = 0; i < 16; i = i + 1) begin
                cud_line[w][i*16 +: 16] = f16i(100 + w*16 + i);
            end
        end

        cud_desc(8'd0, 16'd64, 8'd1, 1'b1);     // L1 word 64, two flits, signal
        send_data(cud_line[0], 1'b0);
        send_data(cud_line[1], 1'b1);

        spin = 0;
        while ((dr_count < 1) && (spin < 2000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(dr_count, 1, "signal_on_complete must report DATA_RECEIVED");
        chk({32'd0, last_dr_arg}, 64'd0, "DATA_RECEIVED arg is the buf_id");

        do_run(9'd80);
        spin = 0;
        while ((sig_count < 75) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 75, "fourth kernel retired");
        chk({31'd0, dbg_fault}, 64'd0, "fourth kernel must not fault");
        chk({31'd0, last_sig_fault}, 64'd0, "a peer write is not a fault");
        for (w = 0; w < 2; w = w + 1) begin
            for (i = 0; i < 16; i = i + 1) begin
                chk({48'd0, dram[(A_DST3 >> 5) + w][i*16 +: 16]},
                    {48'd0, cud_line[w][i*16 +: 16]},
                    "what the peer wrote is what L1 held");
            end
        end

        $display("--- 7. CU_DATA naming a buffer this core does not have ---");
        // One flat L1, so buf_id 3 addresses nothing. It aims at the words
        // section 6 just wrote, so re-draining them afterwards is what proves
        // the burst was dropped rather than merely unreported.
        cud_desc(8'd3, 16'd64, 8'd0, 1'b0);
        send_data({256{1'b1}}, 1'b1);
        repeat (20) @(negedge clk);

        do_run(9'd80);
        spin = 0;
        while ((sig_count < 76) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 76, "the run after a rejected burst retired");
        chk({31'd0, last_sig_fault}, 64'd1, "a rejected burst must fault");
        chk({32'd0, last_sig_arg}, 64'd9, "fault code is F_CUDATA");

        for (w = 0; w < 2; w = w + 1) begin
            dram[(A_DST3 >> 5) + w] = 256'd0;
        end
        do_run(9'd80);
        spin = 0;
        while ((sig_count < 77) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 77, "the core recovers and runs again");
        chk({31'd0, last_sig_fault}, 64'd0, "the fault must not be sticky");
        for (w = 0; w < 2; w = w + 1) begin
            for (i = 0; i < 16; i = i + 1) begin
                chk({48'd0, dram[(A_DST3 >> 5) + w][i*16 +: 16]},
                    {48'd0, cud_line[w][i*16 +: 16]},
                    "a rejected burst must not have touched L1");
            end
        end

        $display("--- 8. vec -> vec: a peer drain, looped back ---");
        put_imem(9'd90, I_VDRAIN_ND);
        put_imem(9'd91, I_VHALT);
        put_imem(9'd92, I_VDRAIN_BACK);
        put_imem(9'd93, I_VHALT);
        put_desc(3'd1, 3'd0, 34'd200);              // peer L1 word 200
        put_desc(3'd1, 3'd1, {18'd1, 16'd2});       // two words

        do_run(9'd90);
        spin = 0;
        // DATA_RECEIVED wins the send arbiter, so it lands BEFORE the kernel's
        // own retire; waiting on either alone races the other.
        while (((dr_count < 2) || (sig_count < 85)) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(dr_count, 2, "the peer drain must carry signal_on_complete");
        chk(cud_out, 3, "one descriptor flit and two data flits");
        chk(sig_count, 85, "fifth kernel retired");
        chk({31'd0, dbg_fault}, 64'd0, "fifth kernel must not fault");

        for (w = 0; w < 2; w = w + 1) begin
            dram[(A_DST3 >> 5) + w] = 256'd0;
        end
        do_run(9'd92);
        spin = 0;
        while ((sig_count < 86) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 86, "sixth kernel retired");
        for (w = 0; w < 2; w = w + 1) begin
            for (i = 0; i < 16; i = i + 1) begin
                chk({48'd0, dram[(A_DST3 >> 5) + w][i*16 +: 16]},
                    {48'd0, cud_line[w][i*16 +: 16]},
                    "L1 -> NoC -> L1 must be the identity");
            end
        end

        $display("--- 9. two senders' bursts interleaved ---");
        // One descriptor and one pointer, so a second sender's flits cannot be
        // told apart from the first's by content and would merge silently. They
        // CAN be told apart by source, and a fault beats a wrong answer.
        cud_desc(8'd0, 16'd300, 8'd1, 1'b0);
        send_data(cud_line[0], 1'b0);
        send_data_from(4'd2, 4'd2, cud_line[1], 1'b1);
        repeat (20) @(negedge clk);

        do_run(9'd92);
        spin = 0;
        while ((sig_count < 87) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 87, "the run after an interleaved burst retired");
        chk({31'd0, last_sig_fault}, 64'd1, "an interleaved burst must fault");
        chk({32'd0, last_sig_arg}, 64'd9, "fault code is F_CUDATA");
        chk(dr_count, 2, "a corrupted burst must not be acknowledged");

        $display("--- 10. the completion goes where the descriptor says ---");
        // The ack destination and the burst's SOURCE are different facts. They
        // shared one register until this section: redirecting the ack then made
        // every data flit of that burst read as a second sender's and the whole
        // transfer was dropped as interleaved.
        // A CU answering its SENDER is useless when the sender is another CU:
        // nothing there consumes it. The ack destination is what lets the host
        // sequence a reader behind a writer without the data coming back.
        cud_desc_ack(8'd0, 16'd320, 8'd0, 1'b1, {4'd2, 4'd1});   // -> (1,2)
        send_data(cud_line[0], 1'b1);
        spin = 0;
        while ((dr_count < 3) && (spin < 2000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(dr_count, 3, "an ack destination still reports");
        chk({56'd0, last_sig_dx, last_sig_dy}, {56'd0, 4'd1, 4'd2},
            "the ack goes to the descriptor's ack field, not to the sender");

        // Zero is the sentinel, and it is unambiguous because (0,0) is a mesh
        // CORNER: it touches no router and can never hold an endpoint.
        cud_desc_ack(8'd0, 16'd320, 8'd0, 1'b1, 8'd0);
        send_data(cud_line[0], 1'b1);
        spin = 0;
        while ((dr_count < 4) && (spin < 2000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(dr_count, 4, "ack 0 still reports");
        chk({56'd0, last_sig_dx, last_sig_dy}, {56'd0, HX[3:0], HY[3:0]},
            "ack 0 means the descriptor's source, exactly as before");

        $display("--- 11. a peer drain carries an ack destination too ---");
        // The instruction word has one bit left, not eight, so the ack rides in
        // the descriptor BASE at [23:16] -- of which a peer drain uses 16.
        put_desc(3'd1, 3'd0, {10'd0, 8'h34, 16'd240});   // ack (4,3), L1 240
        do_run(9'd90);
        spin = 0;
        while ((cud_out < 6) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk({56'd0, last_cud_desc[215 -: 8]}, {56'd0, 8'h34},
            "the emitted descriptor carries {ack_y, ack_x}");
        chk({48'd0, last_cud_desc[247 -: 16]}, {48'd0, 16'd240},
            "and the offset is still the base's low half");

        $display("--- 12. streamed fills: runs, the tag wrap, overlap ---");
        // A: three unbarriered fills (across word 256; strided, its tags aliasing the
        // first's bank-1 tags in flight; long). B: 256 words, hitting the 255 cap.
        repeat (300) @(negedge clk);
        for (w = 512; w < 1024; w = w + 1) begin
            dram[w] = pat(w);
        end
        s0 = sig_count;
        put_imem(9'd100, 32'hE80000C8);                 // VFILL A0 -> L1 200
        put_imem(9'd101, 32'hE8200000);                 // VFILL A1 -> L1 0
        put_imem(9'd102, 32'hE8400140);                 // VFILL A2 -> L1 320
        put_imem(9'd103, I_VBAR);
        put_imem(9'd104, 32'hF0600000);                 // VDRAIN A3 from L1 0
        put_imem(9'd105, 32'hF0A00100);                 // VDRAIN A5 from L1 256
        put_imem(9'd106, I_VHALT);
        put_imem(9'd110, 32'hE8800000);                 // VFILL A4 -> L1 0
        put_imem(9'd111, I_VBAR);
        put_imem(9'd112, 32'hF0600000);
        put_imem(9'd113, I_VHALT);
        put_desc(3'd0, 3'd0, 34'd512 << 5);
        put_desc(3'd0, 3'd1, {18'd32, 16'd120});
        put_desc(3'd1, 3'd0, 34'd640 << 5);
        put_desc(3'd1, 3'd1, {18'd64, 16'd20});
        put_desc(3'd2, 3'd0, 34'd700 << 5);
        put_desc(3'd2, 3'd1, {18'd32, 16'd191});
        put_desc(3'd3, 3'd0, 34'd0);
        put_desc(3'd3, 3'd1, {18'd32, 16'd256});
        put_desc(3'd4, 3'd0, 34'd512 << 5);
        put_desc(3'd4, 3'd1, {18'd32, 16'd256});
        put_desc(3'd5, 3'd0, 34'd256 << 5);
        put_desc(3'd5, 3'd1, {18'd32, 16'd256});

        do_run(9'd100);
        spin = 0;
        while ((sig_count < s0 + 24) && (spin < 120000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, s0 + 24, "kernel A retired");
        chk({31'd0, dbg_fault}, 64'd0, "kernel A must not fault");
        $display("    kernel A retired in %0d cycles", last_sig_arg);
        for (w = 0; w < 120; w = w + 1) fill_chk(200 + w, 512 + w, "fill across word 256");
        for (w = 0; w < 20; w = w + 1) fill_chk(w, 640 + 2*w, "strided fill");
        for (w = 0; w < 191; w = w + 1) fill_chk(320 + w, 700 + w, "long contiguous fill");

        do_run(9'd110);
        spin = 0;
        while ((sig_count < s0 + 25) && (spin < 120000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, s0 + 25, "kernel B retired");
        chk({31'd0, dbg_fault}, 64'd0, "kernel B must not fault");
        $display("    kernel B retired in %0d cycles", last_sig_arg);
        for (w = 0; w < 256; w = w + 1) fill_chk(w, 512 + w, "256-word fill");

        $display("--- 13. a word a cycle: FP32 VLD/VST, strided, bursts, backpressure ---");
        // C runs under link backpressure: an FP32 round trip into a stride-2 L1
        // walk, then a misaligned drain (bursts 5,8,8,8,2) and a stride-64 one
        // (bursts of 1). D, unthrottled, is the rate: 8-word VLD and VST and a
        // 256-word aligned drain, bounded by TB_KD_BOUND.
        for (w = 0; w < 16; w = w + 1) dram[w] = f32line(w);
        for (w = 64; w < 240; w = w + 1) dram[w] = 256'd0;
        s0 = sig_count;
        put_imem(9'd120, I_VSETI);
        put_imem(9'd121, 32'd128);
        put_imem(9'd122, I_VSETVL);
        put_imem(9'd123, I_VSETMD);
        put_imem(9'd124, 32'hE8000000);                 // VFILL A0 -> L1 0
        put_imem(9'd125, I_VBAR);
        put_imem(9'd126, 32'hA2220000);                 // VLD.FP32 v1 <- A1
        put_imem(9'd127, 32'hAA420000);                 // VST.FP32 v1 -> A2
        put_imem(9'd128, 32'hF0800064);                 // VDRAIN A4 from L1 100
        put_imem(9'd129, 32'hF0A00064);                 // VDRAIN A5 from L1 100
        put_imem(9'd130, I_VHALT);
        put_imem(9'd140, 32'hA1260000);                 // VLD.FP16 v3 <- A1
        put_imem(9'd141, 32'hA9C60000);                 // VST.FP16 v3 -> A6
        put_imem(9'd142, 32'hF0E00000);                 // VDRAIN A7 from L1 0
        put_imem(9'd143, I_VHALT);
        put_desc(3'd0, 3'd0, 34'd0);
        put_desc(3'd0, 3'd1, {18'd32, 16'd16});
        put_desc(3'd1, 3'd0, 34'd0);
        put_desc(3'd1, 3'd1, {18'd1, 16'd16});
        put_desc(3'd2, 3'd0, 34'd100);
        put_desc(3'd2, 3'd1, {18'd2, 16'd16});
        put_desc(3'd4, 3'd0, 34'd67 << 5);
        put_desc(3'd4, 3'd1, {18'd32, 16'd31});
        put_desc(3'd5, 3'd0, 34'd200 << 5);
        put_desc(3'd5, 3'd1, {18'd64, 16'd16});
        put_desc(3'd6, 3'd0, 34'd300);
        put_desc(3'd6, 3'd1, {18'd1, 16'd8});
        put_desc(3'd7, 3'd0, 34'd256 << 5);
        put_desc(3'd7, 3'd1, {18'd32, 16'd256});

        bp_on = 1;
        i = n_wburst; j = n_wword;
        do_run(9'd120);
        spin = 0;
        // 15 imem words + 14 descriptor fields + the run
        while ((sig_count < s0 + 30) && (spin < 120000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, s0 + 30, "kernel C retired");
        chk({31'd0, dbg_fault}, 64'd0, "kernel C must not fault");
        $display("    kernel C retired in %0d cycles", last_sig_arg);
        chk(n_wburst - i, 21, "kernel C: bursts 5,8,8,8,2 and 16 single words");
        chk(n_wword - j, 47, "kernel C: words written");
        for (w = 0; w < 31; w = w + 1) begin
            chk({63'd0, dram[67 + w] === l1x(100 + w)}, 64'd1, "misaligned contiguous drain");
        end
        for (w = 0; w < 16; w = w + 1) begin
            chk({63'd0, dram[200 + 2*w] === l1x(100 + w)}, 64'd1, "stride-64 drain");
            if (w < 15) chk({63'd0, dram[201 + 2*w] === 256'd0}, 64'd1, "stride-64 gaps untouched");
        end

        bp_on = 0;
        repeat (20) @(negedge clk);
        i = n_wburst;
        do_run(9'd140);
        spin = 0;
        while ((sig_count < s0 + 31) && (spin < 120000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, s0 + 31, "kernel D retired");
        chk({31'd0, dbg_fault}, 64'd0, "kernel D must not fault");
        $display("    kernel D retired in %0d cycles", last_sig_arg);
        chk({63'd0, last_sig_arg < `TB_KD_BOUND}, 64'd1, "kernel D at a word a cycle");
        chk(n_wburst - i, 32, "kernel D: 256 aligned words are 32 bursts");
        for (w = 0; w < 256; w = w + 1) begin
            chk({63'd0, dram[256 + w] === l1x(w)}, 64'd1, "256-word drain");
        end
        chk(n_wbad, 0, "no malformed write burst anywhere");

        $display("--- 14. back-to-back ALU ops of different shape ---");
        // VADD v7=v0+v1 then VMUL v8=v0*K2 (-1): the second's decode must not reach
        // the first's last beat, which the lanes evaluate a cycle after issue.
        for (w = 0; w < 8; w = w + 1) begin
            for (i = 0; i < 16; i = i + 1) begin
                line[i*16 +: 16] = f16i(w*16 + i + 1);
            end
            dram[(A_SRC >> 5) + w] = line;
            for (i = 0; i < 16; i = i + 1) begin
                line[i*16 +: 16] = f16i(2*(w*16 + i + 1));
            end
            dram[(A_SRC >> 5) + 8 + w] = line;
        end
        s0 = sig_count;
        put_imem(9'd150, 32'hE8000000);                 // VFILL A0 -> L1 0
        put_imem(9'd151, I_VBAR);
        put_imem(9'd152, I_VLD_A1);                     // v0 <- L1 0..7
        put_imem(9'd153, I_VLD_A2);                     // v1 <- L1 8..15
        put_imem(9'd154, 32'h180E0220);                 // VADD v7 = v0 + v1
        put_imem(9'd155, 32'h29900400);                 // VMUL v8 = v0 * K2
        put_imem(9'd156, 32'hA96E0000);                 // VST v7 -> A3
        put_imem(9'd157, 32'hA9900000);                 // VST v8 -> A4
        put_imem(9'd158, 32'hF0A00010);                 // VDRAIN A5 from L1 16
        put_imem(9'd159, I_VHALT);
        put_desc(3'd0, 3'd0, A_SRC);
        put_desc(3'd0, 3'd1, {18'd32, 16'd16});
        put_desc(3'd1, 3'd0, 34'd0);
        put_desc(3'd1, 3'd1, {18'd1, 16'd8});
        put_desc(3'd2, 3'd0, 34'd8);
        put_desc(3'd2, 3'd1, {18'd1, 16'd8});
        put_desc(3'd3, 3'd0, 34'd16);
        put_desc(3'd3, 3'd1, {18'd1, 16'd8});
        put_desc(3'd4, 3'd0, 34'd24);
        put_desc(3'd4, 3'd1, {18'd1, 16'd8});
        put_desc(3'd5, 3'd0, 34'd640 << 5);
        put_desc(3'd5, 3'd1, {18'd32, 16'd16});
        do_run(9'd150);
        spin = 0;
        // 10 imem words + 12 descriptor fields + the run
        while ((sig_count < s0 + 23) && (spin < 120000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, s0 + 23, "kernel E retired");
        chk({31'd0, dbg_fault}, 64'd0, "kernel E must not fault");
        for (w = 0; w < 16; w = w + 1) begin
            for (i = 0; i < 16; i = i + 1) begin
                got16  = dram[640 + w][i*16 +: 16];
                want16 = (w < 8) ? f16i(3 * (w*16 + i + 1))
                                 : (f16i((w-8)*16 + i + 1) | 16'h8000);
                chk({48'd0, got16}, {48'd0, want16}, "VADD then VMUL by K2");
            end
        end

        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #900000;
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire
