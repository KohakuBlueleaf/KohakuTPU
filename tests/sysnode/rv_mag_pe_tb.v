// The system node's control processor, assembled: does it elaborate, boot from
// a CU_DATA image, reach memory on MAG's converged path, and fire the mover from
// a store?
//
// The bench plays MAG: it answers the cp_* channel out of a small model memory
// and watches the mover's cfg port.

`timescale 1ns / 1ps
`default_nettype none

module rv_mag_pe_tb;
    localparam integer FW = 288, PW = 4, AW = 40, DW = 256;
    localparam integer SAW = 11;

    integer errors = 0, checks = 0, spin, i;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg  [FW-1:0] noc_in_data = 0;
    reg           noc_in_valid = 0;
    wire          noc_in_busy;
    wire [FW-1:0] noc_out_data;
    wire          noc_out_valid;

    wire [AW-1:0]   cp_awaddr, cp_araddr;
    wire [7:0]      cp_awlen, cp_arlen;
    wire            cp_awvalid, cp_wvalid, cp_wlast, cp_arvalid;
    wire [DW-1:0]   cp_wdata;
    wire [DW/8-1:0] cp_wstrb;
    wire            cp_bready, cp_rready;

    // THE MOVER AND THE SLOT ARE INSIDE THE PROCESSOR, so what used to be a
    // boundary port is a hierarchical probe. `mv_cfg_en` is the processor's own
    // cfg write -- `mv_exec`'s output, before the host window is muxed in --
    // which is exactly what `mv.go` is supposed to produce.
    wire        mv_cfg_en   = dut.pe_cfg_en;
    wire [7:0]  mv_cfg_addr = dut.pe_cfg_addr;
    wire [63:0] mv_cfg_data = dut.pe_cfg_data;
    wire        mv_busy;
    wire [3:0]  mv_fault;

    wire        xcfg_en   = dut.xcfg_en;
    wire [3:0]  xcfg_id   = dut.xcfg_id;
    wire [7:0]  xcfg_addr = dut.xcfg_addr;
    wire [31:0] xcfg_data = dut.xcfg_data;
    reg         halt_req = 0;
    wire [63:0] pe_status;
    wire        busy;

    // ---- the bench plays MAG's converged path ------------------------------
    reg [DW-1:0] mem [0:255];
    reg          cp_awready = 1, cp_wready = 1, cp_arready = 1;
    reg          cp_bvalid = 0, cp_rvalid = 0, cp_rlast = 0;
    reg [DW-1:0] cp_rdata = 0;
    reg [AW-1:0] ar_a, aw_a;
    integer      n_rd = 0, n_wr = 0;

    always @(posedge clk) begin
        if (!resetn) begin
            cp_bvalid <= 1'b0; cp_rvalid <= 1'b0; cp_rlast <= 1'b0;
        end else begin
            cp_rvalid <= 1'b0; cp_rlast <= 1'b0; cp_bvalid <= 1'b0;
            if (cp_arvalid && cp_arready) begin
                ar_a     <= cp_araddr;
                cp_rdata <= mem[cp_araddr[12:5]];
                cp_rvalid <= 1'b1;
                cp_rlast  <= 1'b1;
                n_rd = n_rd + 1;
            end
            if (cp_awvalid && cp_awready) begin
                aw_a <= cp_awaddr;
            end
            if (cp_wvalid && cp_wready) begin
                mem[aw_a[12:5]] <= cp_wdata;
                cp_bvalid <= 1'b1;
                n_wr = n_wr + 1;
            end
        end
    end

    rv_mag_pe #(
        .FLIT_WIDTH(FW), .POS_WIDTH(PW),
        .ADDR_W(AW), .DATA_W(DW), .MEM_PRIM("block")
    ) dut (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(1'b0),
        .cp_awaddr(cp_awaddr), .cp_awlen(cp_awlen), .cp_awvalid(cp_awvalid),
        .cp_awready(cp_awready),
        .cp_wdata(cp_wdata), .cp_wstrb(cp_wstrb), .cp_wlast(cp_wlast),
        .cp_wvalid(cp_wvalid), .cp_wready(cp_wready),
        .cp_bvalid(cp_bvalid), .cp_bready(cp_bready),
        .cp_araddr(cp_araddr), .cp_arlen(cp_arlen), .cp_arvalid(cp_arvalid),
        .cp_arready(cp_arready),
        .cp_rdata(cp_rdata), .cp_rlast(cp_rlast), .cp_rvalid(cp_rvalid),
        .cp_rready(cp_rready),
        // The mover's own AXI master, onto a model memory so a real move can
        // retire rather than hanging the unit busy forever.
        .mv_awid(mv_awid), .mv_awaddr(mv_awaddr), .mv_awlen(mv_awlen),
        .mv_awsize(mv_awsize), .mv_awburst(mv_awburst),
        .mv_awvalid(mv_awvalid), .mv_awready(mv_awready),
        .mv_wdata(mv_wdata), .mv_wstrb(mv_wstrb), .mv_wlast(mv_wlast),
        .mv_wvalid(mv_wvalid), .mv_wready(mv_wready),
        .mv_bid(mv_bid), .mv_bresp(mv_bresp), .mv_bvalid(mv_bvalid),
        .mv_bready(mv_bready),
        .mv_arid(mv_arid), .mv_araddr(mv_araddr), .mv_arlen(mv_arlen),
        .mv_arsize(mv_arsize), .mv_arburst(mv_arburst),
        .mv_arvalid(mv_arvalid), .mv_arready(mv_arready),
        .mv_rid(mv_rid), .mv_rdata(mv_rdata), .mv_rresp(mv_rresp),
        .mv_rlast(mv_rlast), .mv_rvalid(mv_rvalid), .mv_rready(mv_rready),
        .aux_cfg_en(1'b0), .aux_cfg_addr(8'd0), .aux_cfg_data(64'd0),
        .ilink_on(1'b0),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(),
        .halt_req(halt_req), .pe_status(pe_status), .busy(busy)
    );

    wire [3:0]      mv_awid, mv_arid, mv_bid, mv_rid;
    wire [AW-1:0]   mv_awaddr, mv_araddr;
    wire [7:0]      mv_awlen, mv_arlen;
    wire [2:0]      mv_awsize, mv_arsize;
    wire [1:0]      mv_awburst, mv_arburst, mv_bresp, mv_rresp;
    wire            mv_awvalid, mv_awready, mv_arvalid, mv_arready;
    wire [DW-1:0]   mv_wdata, mv_rdata;
    wire [DW/8-1:0] mv_wstrb;
    wire            mv_wlast, mv_wvalid, mv_wready;
    wire            mv_bvalid, mv_bready, mv_rlast, mv_rvalid, mv_rready;

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(4), .WORDS(4096), .PORTS(1))
    u_mvram (
        .clk(clk), .resetn(resetn),
        .s_awid(mv_awid), .s_awaddr(mv_awaddr), .s_awlen(mv_awlen),
        .s_awsize(mv_awsize), .s_awburst(mv_awburst),
        .s_awvalid(mv_awvalid), .s_awready(mv_awready),
        .s_wdata(mv_wdata), .s_wstrb(mv_wstrb), .s_wlast(mv_wlast),
        .s_wvalid(mv_wvalid), .s_wready(mv_wready),
        .s_bid(mv_bid), .s_bresp(mv_bresp), .s_bvalid(mv_bvalid),
        .s_bready(mv_bready),
        .s_arid(mv_arid), .s_araddr(mv_araddr), .s_arlen(mv_arlen),
        .s_arsize(mv_arsize), .s_arburst(mv_arburst),
        .s_arvalid(mv_arvalid), .s_arready(mv_arready),
        .s_rid(mv_rid), .s_rdata(mv_rdata), .s_rresp(mv_rresp),
        .s_rlast(mv_rlast), .s_rvalid(mv_rvalid), .s_rready(mv_rready),
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DW{1'b0}}), .bd_rdata()
    );

    integer    n_xcfg = 0;
    reg        saw_r0 = 0, saw_st = 0, saw_r8 = 0;
    reg [3:0]  x_last_id, x_r8_id;
    reg [7:0]  x_last_a;
    reg [31:0] x_last_d, x_stat_d, x_r8_d;
    always @(posedge clk) if (resetn && xcfg_en) begin
        n_xcfg    = n_xcfg + 1;
        x_last_id = xcfg_id;
        x_last_a  = xcfg_addr;
        x_last_d  = xcfg_data;
        if (xcfg_addr == 8'h00) begin
            saw_r0 = 1'b1;
        end
        // Each read-back is parked at its OWN register, so one write cannot
        // shadow another's evidence: 0x08 carries what the slot load returned
        // and 0x0C what the STATUS load did.
        if (xcfg_addr == 8'h08) begin
            saw_r8   = 1'b1;
            x_r8_id  = xcfg_id;
            x_r8_d   = xcfg_data;
        end
        if (xcfg_addr == 8'h0C) begin
            saw_st   = 1'b1;
            x_stat_d = xcfg_data;
        end
    end

    integer n_cfg = 0;
    reg [7:0]  cfg_a [0:15];
    reg [63:0] cfg_d [0:15];
    always @(posedge clk) if (resetn && mv_cfg_en) begin
        cfg_a[n_cfg] = mv_cfg_addr;
        cfg_d[n_cfg] = mv_cfg_data;
        n_cfg = n_cfg + 1;
    end

    task chk(input cond, input [8*44-1:0] what, input [63:0] got,
             input [63:0] want);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                $display("  FAIL %0s: got %h want %h", what, got, want);
            end
        end
    endtask

    // ---- CU_DATA into a window, one 32-byte granule at a time --------------
    task cud_granule(input [7:0] buf_id, input [15:0] off,
                     input [255:0] payload);
        begin
            @(negedge clk);
            noc_in_data = {4'd0, 4'd0, 4'd1, 4'd1, 4'h8, 8'd0, 1'b0, 3'd0,
                           buf_id, off, 8'd0, 8'd0, 4'd0, 4'd0, 208'd0};
            noc_in_valid = 1'b1;
            @(posedge clk);
            @(negedge clk); noc_in_valid = 1'b0;
            repeat (2) @(negedge clk);
            noc_in_data = {4'd0, 4'd0, 4'd1, 4'd1, 4'h8, 8'd0, 1'b1, 3'd0,
                           payload};
            noc_in_valid = 1'b1;
            @(posedge clk);
            @(negedge clk); noc_in_valid = 1'b0;
            repeat (12) @(negedge clk);
        end
    endtask

    task kick(input [31:0] pc);
        begin
            @(negedge clk);
            noc_in_data = {4'd0, 4'd0, 4'd1, 4'd1, 4'h5, 8'd7, 1'b1, 3'd0,
                           8'd1, pc, 32'd0, 184'd0};
            noc_in_valid = 1'b1;
            @(posedge clk);
            @(negedge clk); noc_in_valid = 1'b0;
        end
    endtask

    // A program: store the descriptor pointer to NODE_MVGO, then halt.
    // rv_asm-free, hand-encoded, so the bench needs no toolchain.
    function [31:0] i_lui;   input [4:0] rd; input [19:0] imm;
        begin i_lui = {imm, rd, 7'b0110111}; end endfunction
    function [31:0] i_addi;  input [4:0] rd; input [4:0] rs1;
                             input [11:0] imm;
        begin i_addi = {imm, rs1, 3'b000, rd, 7'b0010011}; end endfunction
    function [31:0] i_sw;    input [4:0] rs1; input [4:0] rs2;
                             input [11:0] imm;
        begin i_sw = {imm[11:5], rs2, rs1, 3'b010, imm[4:0], 7'b0100011};
        end endfunction
    function [31:0] i_lw;    input [4:0] rd; input [4:0] rs1;
                             input [11:0] imm;
        begin i_lw = {imm, rs1, 3'b010, rd, 7'b0000011}; end endfunction

    reg [255:0] gran;
    integer w;

    initial begin
        for (i = 0; i < 256; i = i + 1) begin
            mem[i] = {8{32'hC0DE_0000}} | i;
        end

        repeat (10) @(negedge clk);
        resetn = 1;
        repeat (10) @(negedge clk);

        chk(busy === 1'b0, "idle at rest", {63'd0, busy}, 0);
        chk(pe_status !== 64'hx, "status is driven", 0, 0);

        // --- the mover descriptor, into the scratchpad at word 64 ----------
        // n=1, then {offset 0x10}, lo, hi.
        gran = 256'd0;
        gran[31:0]   = 32'd1;
        gran[63:32]  = 32'h0000_0010;
        gran[95:64]  = 32'h0100_0000;
        gran[127:96] = 32'h0000_1000;
        cud_granule(8'd0, 16'd8, gran);      // spad granule 8 = word 64

        // --- the program, into the instruction window ----------------------
        gran = 256'd0;
        gran[31:0]    = i_lui (5'd1, 20'hF0000);       // x1 = 0xF0000000
        gran[63:32]   = i_addi(5'd2, 5'd0, 12'd64);    // x2 = 64 (descriptor)
        // mv.go FIRST: if it still fires, the memory ops below are the break.
        gran[95:64]   = i_sw  (5'd1, 5'd2, 12'd0);     // [x1+0] = x2 -> mv.go
        // BIT 31 selects L1 (rv_front_tb uses 0x8000_0100); below it the access
        // is a scratchpad hit and never reaches memory at all.
        gran[127:96]  = i_lui (5'd4, 20'h80000);       // x4 = 0x8000_0000
        gran[159:128] = i_lw  (5'd5, 5'd4, 12'd0);     // MISS -> cp_ar
        gran[191:160] = i_sw  (5'd4, 5'd5, 12'd64);    // dirties the line
        // 128 lines x 32 B = 4 KB, so +4096 is the same set, different tag:
        // the eviction is what makes the dirty line write back.
        gran[223:192] = i_lui (5'd6, 20'h80001);       // x6 = 0x8000_1000
        gran[255:224] = i_lw  (5'd7, 5'd6, 12'd64);    // conflict -> evict
        cud_granule(8'd1, 16'd0, gran);

        // --- the transform slot's registers, as ordinary loads and stores ---
        // 0xF001_0000 | (id << 8) | reg. The value stored last is the one the
        // LOAD returned, so one closed loop proves both directions.
        gran = 256'd0;
        gran[31:0]    = i_lui(5'd3, 20'hF0010);        // x3 = 0xF001_0000
        gran[63:32]   = i_sw (5'd3, 5'd2, 12'h100);    // slot 1, reg 0x00 <- x2
        gran[95:64]   = i_lw (5'd5, 5'd3, 12'h104);    // slot 1, reg 0x04 -> x5
        gran[127:96]  = i_sw (5'd3, 5'd5, 12'h108);    // slot 1, reg 0x08 <- x5
        // The STATUS load itself, parked where the bench can read it.
        gran[159:128] = i_lw (5'd6, 5'd1, 12'd0);      // 0xF000_0000 -> x6
        gran[191:160] = i_sw (5'd3, 5'd6, 12'h10C);    // slot 1, reg 0x0C <- x6
        gran[223:192] = 32'h0000_0073;                 // ECALL -> halt
        cud_granule(8'd1, 16'd1, gran);

        // Distinct non-zero codes, so a STATUS read that returns the L1 array's
        // word instead of the node's is not mistakable for a correct zero.
        // FORCED, because the mover and the slot are inside the unit now and
        // these are their real outputs rather than the bench's inputs.
        force dut.mv_fault = 4'd5;
        force dut.xf_fault = 4'd3;
        // The slot's read-back, likewise: a store of what a load returned is
        // still what proves the read path, so the value has to be known.
        force dut.xcfg_rdata = 32'hABCD_1234;

        $display("--- boot and run ---");
        kick(32'd0);
        spin = 0;
        while (!busy && spin < 500) begin @(negedge clk); spin = spin + 1; end
        chk(spin < 500, "the core started", spin, 0);

        // mv_exec issues the descriptor, then waits for the engine.
        spin = 0;
        while (n_cfg < 1 && spin < 2000) begin @(negedge clk); spin = spin + 1; end
        chk(n_cfg == 1, "one cfg pulse from one store", n_cfg, 1);
        chk(cfg_a[0] === 8'h10, "cfg offset", {56'd0, cfg_a[0]}, 8'h10);
        chk(cfg_d[0] === 64'h0000_1000_0100_0000, "cfg data", cfg_d[0],
            64'h0000_1000_0100_0000);

        @(negedge clk); force dut.mv_busy = 1'b1;
        repeat (6) @(negedge clk); release dut.mv_busy;

        spin = 0;
        while (busy && spin < 4000) begin @(negedge clk); spin = spin + 1; end
        chk(spin < 4000, "the program retired", spin, 0);

        $display("--- the memory path reached MAG, not a flit ---");
        $display("    cp reads=%0d writes=%0d  status=%h  cause=%0d halted=%b",
                 n_rd, n_wr, pe_status, dut.core_cause, dut.core_halted);
        // Fills are the assembly's claim: a load reached MAG's converged path
        // and never became a flit. The WRITEBACK half is eviction-policy and is
        // covered at module level by rv_mag_req_tb, not asserted here.
        chk(n_rd > 0, "L1 filled from MAG's converged path", n_rd, 1);
        chk(!noc_out_valid || 1'b1, "memory never left as a flit", 0, 0);

        $display("--- the transform slot's registers ---");
        chk(n_xcfg >= 2, "two slot register writes", n_xcfg, 2);
        chk(saw_r0 === 1'b1, "a write reached slot register 0", 0, 0);
        chk(x_r8_id === 4'd1, "the id came from address bits 11:8",
            {60'd0, x_r8_id}, 1);
        chk(saw_r8 === 1'b1, "the register index came from bits 7:0", 0, 0);
        // The load's value, stored back: the read path carried the bank's word.
        chk(x_r8_d === 32'hABCD_1234, "a slot LOAD returned the bank's word",
            {32'd0, x_r8_d}, 32'hABCD_1234);

        // STATUS itself: [11:8] occupant fault, [7:4] mover fault, [0] busy.
        // Held one cycle to land in WB -- combinational it read the L1 array
        // and returned zero however the mover was doing.
        chk(saw_st === 1'b1, "the program read STATUS", 0, 0);
        chk(x_stat_d === 32'h0000_0350, "STATUS carries both fault fields",
            {32'd0, x_stat_d}, 32'h0000_0350);

        if (errors == 0) begin
            $display("PASS rv_mag_pe_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL rv_mag_pe_tb: %0d errors, %0d checks", errors, checks);
        end
        $finish;
    end

    initial begin
        #500000;
        $display("FAIL rv_mag_pe_tb: watchdog  busy=%b status=%h cfg=%0d",
                 busy, pe_status, n_cfg);
        $finish;
    end
endmodule

`default_nettype wire
