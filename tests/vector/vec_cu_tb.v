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

module vec_cu_tb;
    localparam FW = 288;
    localparam PW = 4;
    localparam CX = 3, CY = 3;      // the vector CU
    localparam HX = 0, HY = 0;      // us, the agent
    localparam MX = 1, MY = 1;      // us again, as memory

    localparam [3:0] T_MEM_RD_REQ  = 4'h0, T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2, T_MEM_WR_DATA = 4'h4,
                     T_CU_INST = 4'h5, T_CU_SIGNAL = 4'h6;

    localparam A_SRC = 34'h1000, A_DST = 34'h2000, A_DST2 = 34'h3000;

    reg clk = 0, resetn = 0;
    always #2 clk = ~clk;

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
             .L1_DEPTH(512), .L1_PRIM("block")) dut (
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
                if (errors < 20)
                    $display("  FAIL %0s: got %0h want %0h", what, got, want);
            end
        end
    endtask

    // FP16 for a small non-negative integer. Exact below 2048.
    function [15:0] f16i(input integer v);
        integer t, ex;
        reg [31:0] sh;
        begin
            if (v == 0) f16i = 16'd0;
            else begin
                t = v; ex = 0;
                while (t > 1) begin t = t >> 1; ex = ex + 1; end
                if (ex <= 10) sh = v << (10 - ex);
                else          sh = v >> (ex - 10);
                f16i = {1'b0, (5'd15 + ex[4:0]), sh[9:0]};
            end
        end
    endfunction

    // ================================================ memory model
    // Word addressed: one 256-bit line per 32 bytes. Reads are queued and
    // answered after a delay, so the core cannot depend on a fixed latency.
    reg [255:0] dram [0:1023];
    reg [33:0]  rq_addr [0:63];
    reg [7:0]   rq_tag  [0:63];
    integer     rq_head, rq_tail, rq_wait;
    reg [33:0]  wr_addr_l;
    reg         wr_open;

    reg  [FW-1:0] mem_flit;
    reg           mem_valid;

    wire [3:0] o_type = out_data[FW-4*PW-1 -: 4];
    wire [7:0] o_txn  = out_data[FW-4*PW-5 -: 8];
    wire [33:0] o_addr = out_data[255 -: 34];

    integer sig_count;
    reg [31:0] last_sig_arg;
    reg        last_sig_fault;

    always @(posedge clk) begin
        if (!resetn) begin
            rq_head <= 0; rq_tail <= 0; rq_wait <= 0;
            wr_open <= 1'b0; mem_valid <= 1'b0;
            sig_count <= 0; last_sig_arg <= 32'd0; last_sig_fault <= 1'b0;
        end else begin
            mem_valid <= 1'b0;

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
                    last_sig_fault <= (out_data[255 -: 8] == 8'h04);
                end
                default: ;
                endcase
            end

            // answer one queued read every few cycles
            if (rq_head != rq_tail) begin
                if (rq_wait < 3) rq_wait <= rq_wait + 1;
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
        end else if (agent_valid) begin
            in_valid = 1'b1;
            in_data  = agent_flit;
        end
    end

    task send_cu(input [255:0] payload);
        begin
            @(negedge clk);
            while (in_busy || mem_valid) @(negedge clk);
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

    integer i, j, w, spin;
    reg [255:0] line;
    reg [15:0]  got16, want16;

    initial begin
        agent_valid = 0; agent_flit = 0; out_busy = 0;
        for (i = 0; i < 1024; i = i + 1) dram[i] = 256'd0;

        // a[i] = i+1 at 0x1000, b[i] = 2(i+1) at 0x1000 + 8 lines
        for (w = 0; w < 8; w = w + 1) begin
            line = 256'd0;
            for (i = 0; i < 16; i = i + 1)
                line[i*16 +: 16] = f16i(w*16 + i + 1);
            dram[(A_SRC >> 5) + w] = line;
            line = 256'd0;
            for (i = 0; i < 16; i = i + 1)
                line[i*16 +: 16] = f16i(2*(w*16 + i + 1));
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
        if (spin >= 60000) $display("  FAIL kernel never retired");
        $display("    kernel retired in %0d cycles", last_sig_arg);

        $display("--- 3. what landed in memory ---");
        for (w = 0; w < 24; w = w + 1) begin
            line = dram[(A_DST >> 5) + w];
            for (i = 0; i < 16; i = i + 1) begin
                got16 = line[i*16 +: 16];
                if (w < 8)       want16 = f16i(w*16 + i + 1);
                else if (w < 16) want16 = f16i(2*((w-8)*16 + i + 1));
                else             want16 = f16i(3*((w-16)*16 + i + 1));
                if (got16 !== want16)
                    $display("    word %0d elem %0d: got %04h want %04h",
                             w, i, got16, want16);
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
        if (spin >= 60000) $display("  FAIL second kernel never retired");
        $display("    second kernel retired in %0d cycles", last_sig_arg);

        for (w = 0; w < 16; w = w + 1) begin
            line = dram[(A_DST2 >> 5) + w];
            for (i = 0; i < 16; i = i + 1) begin
                got16 = line[i*16 +: 16];
                // words 0..7 are the D4 chain, 8..15 the broadcast sum
                want16 = (w < 8) ? f16i(w*16 + i + 3) : f16i(128*129/2);
                if (got16 !== want16)
                    $display("    k2 word %0d elem %0d: got %04h want %04h",
                             w, i, got16, want16);
                chk({48'd0, got16}, {48'd0, want16}, "kernel 2 word");
            end
        end

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
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
