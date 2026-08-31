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
             .L1_DEPTH(512), .L1_PRIM("block"),
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
    integer     rq_head, rq_tail, rq_wait;
    reg [39:0]  wr_addr_l;
    reg         wr_open;

    reg  [FW-1:0] mem_flit;
    reg           mem_valid;

    wire [3:0] o_type = out_data[FW-4*PW-1 -: 4];
    wire [7:0] o_txn  = out_data[FW-4*PW-5 -: 8];
    // 40, NOT 34. NOC_MEM_ADDR is [255:216]; decoding the top 34 reads the
    // address shifted right by six and the drain lands where nothing looks.
    wire [39:0] o_addr = out_data[255 -: 40];

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
            rq_head <= 0; rq_tail <= 0; rq_wait <= 0;
            wr_open <= 1'b0; mem_valid <= 1'b0;
            sig_count <= 0; last_sig_arg <= 32'd0; last_sig_fault <= 1'b0;
            dr_count <= 0; last_dr_arg <= 32'd0;
            lb_head <= 0; lb_tail <= 0; cud_out <= 0; lb_valid <= 1'b0;
            last_sig_dx <= 4'd0; last_sig_dy <= 4'd0;
            last_cud_desc <= 256'd0; cud_left <= 9'd0;
        end else begin
            mem_valid <= 1'b0;
            lb_valid  <= 1'b0;

            if (out_valid) begin
                case (o_type)
                    T_MEM_RD_REQ: begin
                        rq_addr[rq_tail[5:0]] <= o_addr;
                        rq_tag[rq_tail[5:0]]  <= o_txn;
                        rq_tail <= rq_tail + 1;
                    end
                    T_MEM_WR_REQ: begin
                        wr_addr_l <= o_addr;
                        wr_open   <= 1'b1;
                    end
                    T_MEM_WR_DATA: if (wr_open) begin
                        dram[wr_addr_l[14:5]] <= out_data[255:0];
                        wr_open <= 1'b0;
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

            // answer one queued read every few cycles
            if (rq_head != rq_tail) begin
                if (rq_wait < 3) begin
                    rq_wait <= rq_wait + 1;
                end
                else begin
                    rq_wait  <= 0;
                    mem_flit <= { CX[3:0], CY[3:0], MX[3:0], MY[3:0],
                                  T_MEM_RD_RESP, rq_tag[rq_head[5:0]],
                                  1'b1, 3'b000,
                                  dram[rq_addr[rq_head[5:0]][14:5]] };
                    mem_valid <= 1'b1;
                    rq_head   <= rq_head + 1;
                end
            end
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

    integer i, j, w, spin;
    reg [255:0] line;
    reg [255:0] cud_line [0:1];
    reg [15:0]  got16, want16;

    initial begin
        agent_valid = 0; agent_flit = 0; out_busy = 0;
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
        put_imem(9'd40, I_VSETMD);
        put_imem(9'd41, I_VSETI);      put_imem(9'd42, 32'd128);
        put_imem(9'd43, I_VSETVL);
        put_imem(9'd44, I_K_SET);      put_imem(9'd45, 32'h00428000);   // 64.0
        put_imem(9'd46, I_CMPGT_P0);
        put_imem(9'd47, I_VST_A5);
        put_imem(9'd48, I_VSETI);      put_imem(9'd49, 32'd64);
        put_imem(9'd50, I_VSETVL);
        put_imem(9'd51, I_ANY_P0);
        put_imem(9'd52, I_VSETI);      put_imem(9'd53, 32'd128);
        put_imem(9'd54, I_VSETVL);
        put_imem(9'd55, I_K_SET);      put_imem(9'd56, 32'h00428200);   // 65.0
        put_imem(9'd57, I_CMPLT_P1);
        put_imem(9'd58, I_VST_A5);
        put_imem(9'd59, I_VSETI);      put_imem(9'd60, 32'd64);
        put_imem(9'd61, I_VSETVL);
        put_imem(9'd62, I_ALL_P1);
        put_imem(9'd63, I_BC_S3_V8);
        put_imem(9'd64, I_VST_V8_A5);
        put_imem(9'd65, I_BC_S4_V9);
        put_imem(9'd66, I_VST_V9_A6);
        put_imem(9'd67, I_VDRAIN2);
        put_imem(9'd68, I_VHALT);

        do_run(9'd40);
        spin = 0;
        // 29 imem writes on top of 44, then the kernel's own HALT
        while ((sig_count < 74) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 74, "third kernel retired");
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
        while ((sig_count < 81) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 81, "fourth kernel retired");
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
        while ((sig_count < 82) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 82, "the run after a rejected burst retired");
        chk({31'd0, last_sig_fault}, 64'd1, "a rejected burst must fault");
        chk({32'd0, last_sig_arg}, 64'd9, "fault code is F_CUDATA");

        for (w = 0; w < 2; w = w + 1) begin
            dram[(A_DST3 >> 5) + w] = 256'd0;
        end
        do_run(9'd80);
        spin = 0;
        while ((sig_count < 83) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 83, "the core recovers and runs again");
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
        while (((dr_count < 2) || (sig_count < 91)) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(dr_count, 2, "the peer drain must carry signal_on_complete");
        chk(cud_out, 3, "one descriptor flit and two data flits");
        chk(sig_count, 91, "fifth kernel retired");
        chk({31'd0, dbg_fault}, 64'd0, "fifth kernel must not fault");

        for (w = 0; w < 2; w = w + 1) begin
            dram[(A_DST3 >> 5) + w] = 256'd0;
        end
        do_run(9'd92);
        spin = 0;
        while ((sig_count < 92) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 92, "sixth kernel retired");
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
        while ((sig_count < 93) && (spin < 60000)) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(sig_count, 93, "the run after an interleaved burst retired");
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
