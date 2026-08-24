// The system node with its processor generated: does a program loaded over the
// NoC port drive the REAL mover, inside the real MAG, out to real DRAM?
//
// Everything below the node is the shipping RTL -- mag, mm_mover, mag_stage_port,
// mag_dram_port. Only DRAM is a model.

`timescale 1ns / 1ps
`default_nettype none

module sysnode_ctrlpe_tb;
    localparam integer FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4;
    localparam integer NW = 8;
    localparam [AW-1:0] SRC_OFF = 40'h10_0000;
    localparam [AW-1:0] DST_OFF = 40'h20_0000;

    integer errors = 0, checks = 0, spin, i;

    reg clk = 0, dclk = 0, resetn = 0;
    always begin
        #2   clk  = ~clk;
    end
    always begin
        #1.7 dclk = ~dclk;
    end

    reg  [FW-1:0] pe_in_data = 0;
    reg           pe_in_valid = 0;
    wire          pe_in_busy, pe_out_valid, pe_busy;
    wire [FW-1:0] pe_out_data;
    wire [63:0]   pe_status;
    wire          mv_busy;
    wire [3:0]    mv_fault;

    wire [IDW-1:0]  dm_awid, dm_arid, dm_bid, dm_rid;
    wire [AW-1:0]   dm_awaddr, dm_araddr;
    wire [7:0]      dm_awlen, dm_arlen;
    wire [2:0]      dm_awsize, dm_arsize;
    wire [1:0]      dm_awburst, dm_arburst, dm_bresp, dm_rresp;
    wire            dm_awvalid, dm_awready, dm_arvalid, dm_arready;
    wire [DW-1:0]   dm_wdata, dm_rdata;
    wire [DW/8-1:0] dm_wstrb;
    wire            dm_wlast, dm_wvalid, dm_wready;
    wire            dm_bvalid, dm_bready, dm_rlast, dm_rvalid, dm_rready;

    sysnode #(
        .FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW),
        .ID_W(IDW), .MEM_PORTS(1), .ILINK(0), .MW(DW),
        .CTRL_PE(1), .PE_X(1), .PE_Y(0), .PE_MEM_PRIM("block")
    ) dut (
        .clk(clk), .resetn(resetn),
        .sm_awid({IDW{1'b0}}), .sm_awaddr({AW{1'b0}}), .sm_awlen(8'd0),
        .sm_awvalid(1'b0), .sm_awready(),
        .sm_wdata({DW{1'b0}}), .sm_wstrb({(DW/8){1'b0}}), .sm_wlast(1'b0),
        .sm_wvalid(1'b0), .sm_wready(),
        .sm_bid(), .sm_bresp(), .sm_bvalid(), .sm_bready(1'b1),
        .sm_arid({IDW{1'b0}}), .sm_araddr({AW{1'b0}}), .sm_arlen(8'd0),
        .sm_arvalid(1'b0), .sm_arready(),
        .sm_rid(), .sm_rdata(), .sm_rresp(), .sm_rlast(), .sm_rvalid(),
        .sm_rready(1'b1),
        .sc_awid({IDW{1'b0}}), .sc_awaddr(32'd0), .sc_awlen(8'd0),
        .sc_awvalid(1'b0), .sc_awready(),
        .sc_wdata(64'd0), .sc_wstrb(8'd0), .sc_wlast(1'b0), .sc_wvalid(1'b0),
        .sc_wready(),
        .sc_bid(), .sc_bresp(), .sc_bvalid(), .sc_bready(1'b1),
        .sc_arid({IDW{1'b0}}), .sc_araddr(32'd0), .sc_arlen(8'd0),
        .sc_arvalid(1'b0), .sc_arready(),
        .sc_rid(), .sc_rdata(), .sc_rresp(), .sc_rlast(), .sc_rvalid(),
        .sc_rready(1'b1),
        .dram_aclk(dclk), .dram_aresetn(resetn),
        .dram_awid(dm_awid), .dram_awaddr(dm_awaddr), .dram_awlen(dm_awlen),
        .dram_awsize(dm_awsize), .dram_awburst(dm_awburst),
        .dram_awvalid(dm_awvalid), .dram_awready(dm_awready),
        .dram_wdata(dm_wdata), .dram_wstrb(dm_wstrb), .dram_wlast(dm_wlast),
        .dram_wvalid(dm_wvalid), .dram_wready(dm_wready),
        .dram_bid(dm_bid), .dram_bresp(dm_bresp), .dram_bvalid(dm_bvalid),
        .dram_bready(dm_bready),
        .dram_arid(dm_arid), .dram_araddr(dm_araddr), .dram_arlen(dm_arlen),
        .dram_arsize(dm_arsize), .dram_arburst(dm_arburst),
        .dram_arvalid(dm_arvalid), .dram_arready(dm_arready),
        .dram_rid(dm_rid), .dram_rdata(dm_rdata), .dram_rresp(dm_rresp),
        .dram_rlast(dm_rlast), .dram_rvalid(dm_rvalid), .dram_rready(dm_rready),
        .mem_in_data({FW{1'b0}}), .mem_in_valid(1'b0), .mem_in_busy(),
        .mem_out_data(), .mem_out_valid(), .mem_out_busy(1'b0),
        .pe_in_data(pe_in_data), .pe_in_valid(pe_in_valid),
        .pe_in_busy(pe_in_busy),
        .pe_out_data(pe_out_data), .pe_out_valid(pe_out_valid),
        .pe_out_busy(1'b0),
        .mem_rd_count(), .mem_wr_count(),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(),
        .pe_halt_req(1'b0), .pe_status(pe_status), .pe_busy(pe_busy),
        .link0_out_tdata(), .link0_out_tuser(), .link0_out_tlast(),
        .link0_out_tvalid(), .link0_out_tready(1'b1),
        .link0_in_tdata(288'd0), .link0_in_tuser(96'd0), .link0_in_tlast(1'b0),
        .link0_in_tvalid(1'b0), .link0_in_tready(),
        .link1_out_tdata(), .link1_out_tuser(), .link1_out_tlast(),
        .link1_out_tvalid(), .link1_out_tready(1'b1),
        .link1_in_tdata(288'd0), .link1_in_tuser(96'd0), .link1_in_tlast(1'b0),
        .link1_in_tvalid(1'b0), .link1_in_tready()
    );

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .WORDS(70000), .PORTS(1))
    ram (
        .clk(dclk), .resetn(resetn),
        .s_awid(dm_awid), .s_awaddr(dm_awaddr), .s_awlen(dm_awlen),
        .s_awsize(dm_awsize), .s_awburst(dm_awburst),
        .s_awvalid(dm_awvalid), .s_awready(dm_awready),
        .s_wdata(dm_wdata), .s_wstrb(dm_wstrb), .s_wlast(dm_wlast),
        .s_wvalid(dm_wvalid), .s_wready(dm_wready),
        .s_bid(dm_bid), .s_bresp(dm_bresp), .s_bvalid(dm_bvalid),
        .s_bready(dm_bready),
        .s_arid(dm_arid), .s_araddr(dm_araddr), .s_arlen(dm_arlen),
        .s_arsize(dm_arsize), .s_arburst(dm_arburst),
        .s_arvalid(dm_arvalid), .s_arready(dm_arready),
        .s_rid(dm_rid), .s_rdata(dm_rdata), .s_rresp(dm_rresp),
        .s_rlast(dm_rlast), .s_rvalid(dm_rvalid), .s_rready(dm_rready),
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DW{1'b0}}), .bd_rdata()
    );

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

    task cud_granule(input [7:0] buf_id, input [15:0] off,
                     input [255:0] payload);
        begin
            @(negedge clk);
            pe_in_data = {4'd1, 4'd0, 4'd2, 4'd2, 4'h8, 8'd0, 1'b0, 3'd0,
                          buf_id, off, 8'd0, 8'd0, 4'd0, 4'd0, 208'd0};
            pe_in_valid = 1'b1;
            @(posedge clk); @(negedge clk); pe_in_valid = 1'b0;
            repeat (2) @(negedge clk);
            pe_in_data = {4'd1, 4'd0, 4'd2, 4'd2, 4'h8, 8'd0, 1'b1, 3'd0,
                          payload};
            pe_in_valid = 1'b1;
            @(posedge clk); @(negedge clk); pe_in_valid = 1'b0;
            repeat (12) @(negedge clk);
        end
    endtask

    task kick(input [31:0] pc);
        begin
            @(negedge clk);
            pe_in_data = {4'd1, 4'd0, 4'd2, 4'd2, 4'h5, 8'd7, 1'b1, 3'd0,
                          8'd1, pc, 32'd0, 184'd0};
            pe_in_valid = 1'b1;
            @(posedge clk); @(negedge clk); pe_in_valid = 1'b0;
        end
    endtask

    function [31:0] i_lui;  input [4:0] rd; input [19:0] imm;
        begin i_lui = {imm, rd, 7'b0110111}; end endfunction
    function [31:0] i_addi; input [4:0] rd; input [4:0] rs1; input [11:0] imm;
        begin i_addi = {imm, rs1, 3'b000, rd, 7'b0010011}; end endfunction
    function [31:0] i_sw;   input [4:0] rs1; input [4:0] rs2; input [11:0] imm;
        begin i_sw = {imm[11:5], rs2, rs1, 3'b010, imm[4:0], 7'b0100011};
        end endfunction

    reg [255:0] gran;

    initial begin
        // Source words in DRAM, destination poisoned.
        for (i = 0; i < NW; i = i + 1) begin
            ram.mem[(SRC_OFF >> 5) + i] = {8{32'hAC00_0000 | i[31:0]}};
            ram.mem[(DST_OFF >> 5) + i] = {8{32'hA5A5_A5A5}};
        end

        repeat (20) @(negedge clk);
        resetn = 1'b1;
        repeat (60) @(negedge clk);

        chk(pe_busy === 1'b0, "processor idle at rest", {63'd0, pe_busy}, 0);

        // mover.py's program for an 8-word copy, as a descriptor in the spad.
        // 7 writes x 3 words + the count = 22 words, so two granules.
        gran = 256'd0;
        gran[31:0]    = 32'd7;
        gran[63:32]   = 32'h10;
        gran[95:64]   = 32'h0100_0000;   // src hdr lo
        gran[127:96]  = 32'h0000_1000;   // src hdr hi
        gran[159:128] = 32'h18;
        gran[191:160] = 32'h0200_0080;   // count 8 << 4 | stride 32 << 20
        gran[223:192] = 32'h0000_0000;
        gran[255:224] = 32'h20;
        cud_granule(8'd0, 16'd8, gran);

        gran = 256'd0;
        gran[31:0]    = 32'd0;           // axis lo
        gran[63:32]   = 32'd0;           // axis hi
        gran[95:64]   = 32'h10;
        gran[127:96]  = 32'h0200_0001;   // dst hdr lo
        gran[159:128] = 32'h0000_1000;   // dst hdr hi
        gran[191:160] = 32'h18;
        gran[223:192] = 32'h0200_0081;
        gran[255:224] = 32'h0000_0000;
        cud_granule(8'd0, 16'd9, gran);

        gran = 256'd0;
        gran[31:0]    = 32'h20;
        gran[63:32]   = 32'd0;
        gran[95:64]   = 32'd0;
        gran[127:96]  = 32'h00;          // CTRL
        gran[159:128] = 32'h0001_0808;   // COPY W16 WCOAL, go
        gran[191:160] = 32'd0;
        cud_granule(8'd0, 16'd10, gran);

        // The program: hand mv_exec the pointer, then halt.
        gran = 256'd0;
        gran[31:0]   = i_lui (5'd1, 20'hF0000);
        gran[63:32]  = i_addi(5'd2, 5'd0, 12'd64);   // spad word 64 = granule 8
        gran[95:64]  = i_sw  (5'd1, 5'd2, 12'd0);    // mv.go
        gran[127:96] = 32'h0000_0073;                // ECALL
        cud_granule(8'd1, 16'd0, gran);

        $display("--- kick the processor over its own NoC port ---");
        kick(32'd0);
        spin = 0;
        while (!pe_busy && spin < 1000) begin @(negedge clk); spin = spin + 1; end
        chk(spin < 1000, "processor started", spin, 0);

        spin = 0;
        while (!mv_busy && spin < 8000) begin @(negedge clk); spin = spin + 1; end
        chk(spin < 8000, "THE REAL MOVER STARTED, driven by the processor",
            spin, 0);

        spin = 0;
        while (mv_busy && spin < 40000) begin @(negedge clk); spin = spin + 1; end
        chk(spin < 40000, "the mover went idle", spin, 0);
        chk(mv_fault === 4'd0, "no mover fault", {60'd0, mv_fault}, 0);

        repeat (2000) @(negedge clk);

        $display("--- what landed in DRAM ---");
        for (i = 0; i < NW; i = i + 1) begin
            chk(ram.mem[(DST_OFF >> 5) + i] === {8{32'hAC00_0000 | i[31:0]}},
                "destination word", i, i);
        end

        $display("    status=%h", pe_status);

        if (errors == 0) begin
            $display("PASS sysnode_ctrlpe_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL sysnode_ctrlpe_tb: %0d errors, %0d checks",
                     errors, checks);
        end
        $finish;
    end

    initial begin
        #2000000;
        $display("FAIL sysnode_ctrlpe_tb: watchdog  pe_busy=%b mv_busy=%b status=%h",
                 pe_busy, mv_busy, pe_status);
        $finish;
    end
endmodule

`default_nettype wire
