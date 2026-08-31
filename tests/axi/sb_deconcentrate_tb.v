// Self-checking bench for sb_axi_deconcentrate: one AXI master -> 1->N demux -> N
// axi4_ram. Proves address-decoded routing (port = addr[PSEL_LSB+:PW]), response
// return from the held port, and burst handling. Distinct data per port catches a
// mis-route (it would read back from the wrong RAM).

`timescale 1ns/1ps
`default_nettype none

module sb_deconcentrate_tb;
    localparam integer N = 4, DW = 512, AW = 40, IDW = 4, PSEL_LSB = 16;
    localparam integer STRB = DW/8;

    reg clk = 0, rst = 1;
    always begin
        #1.667 clk = ~clk;
    end

    reg  [IDW-1:0] s_awid, s_arid;
    reg  [AW-1:0]  s_awaddr, s_araddr;
    reg  [7:0]     s_awlen, s_arlen;
    reg  [2:0]     s_awsize, s_arsize;
    reg  [1:0]     s_awburst, s_arburst;
    reg            s_awvalid, s_arvalid, s_wvalid, s_wlast, s_bready, s_rready;
    reg  [DW-1:0]  s_wdata;
    reg  [STRB-1:0] s_wstrb;
    wire s_awready, s_wready, s_arready;
    wire [IDW-1:0] s_bid, s_rid;
    wire [1:0]     s_bresp, s_rresp;
    wire           s_bvalid, s_rvalid, s_rlast;
    wire [DW-1:0]  s_rdata;

    wire [N*IDW-1:0] m_awid, m_arid, m_bid, m_rid;
    wire [N*AW-1:0]  m_awaddr, m_araddr;
    wire [N*8-1:0]   m_awlen, m_arlen;
    wire [N*3-1:0]   m_awsize, m_arsize;
    wire [N*2-1:0]   m_awburst, m_arburst, m_bresp, m_rresp;
    wire [N-1:0]     m_awvalid, m_awready, m_wvalid, m_wready, m_wlast;
    wire [N-1:0]     m_bvalid, m_bready, m_arvalid, m_arready, m_rvalid, m_rready, m_rlast;
    wire [N*DW-1:0]  m_wdata, m_rdata;
    wire [N*STRB-1:0] m_wstrb;

    sb_axi_deconcentrate #(.N(N), .DW(DW), .AW(AW), .IDW(IDW), .PSEL_LSB(PSEL_LSB)) dut (
        .clk(clk), .rst(rst),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen), .s_awsize(s_awsize),
        .s_awburst(s_awburst), .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(s_bid), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .s_arid(s_arid), .s_araddr(s_araddr), .s_arlen(s_arlen), .s_arsize(s_arsize),
        .s_arburst(s_arburst), .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(s_rid), .s_rdata(s_rdata), .s_rresp(s_rresp), .s_rlast(s_rlast),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen), .m_awsize(m_awsize),
        .m_awburst(m_awburst), .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid), .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen), .m_arsize(m_arsize),
        .m_arburst(m_arburst), .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_rvalid(m_rvalid), .m_rready(m_rready)
    );

    genvar g;
    generate for (g = 0; g < N; g = g + 1) begin : ram
        axi4_ram #(.DATA_WIDTH(DW), .ADDR_WIDTH(AW), .ID_WIDTH(IDW), .DEPTH(4096)) u (
            .clk(clk), .resetn(~rst),
            .s_axi_awid(m_awid[g*IDW +: IDW]), .s_axi_awaddr(m_awaddr[g*AW +: AW]),
            .s_axi_awlen(m_awlen[g*8 +: 8]), .s_axi_awsize(m_awsize[g*3 +: 3]),
            .s_axi_awburst(m_awburst[g*2 +: 2]), .s_axi_awvalid(m_awvalid[g]),
            .s_axi_awready(m_awready[g]),
            .s_axi_wdata(m_wdata[g*DW +: DW]), .s_axi_wstrb(m_wstrb[g*STRB +: STRB]),
            .s_axi_wlast(m_wlast[g]), .s_axi_wvalid(m_wvalid[g]), .s_axi_wready(m_wready[g]),
            .s_axi_bid(m_bid[g*IDW +: IDW]), .s_axi_bresp(m_bresp[g*2 +: 2]),
            .s_axi_bvalid(m_bvalid[g]), .s_axi_bready(m_bready[g]),
            .s_axi_arid(m_arid[g*IDW +: IDW]), .s_axi_araddr(m_araddr[g*AW +: AW]),
            .s_axi_arlen(m_arlen[g*8 +: 8]), .s_axi_arsize(m_arsize[g*3 +: 3]),
            .s_axi_arburst(m_arburst[g*2 +: 2]), .s_axi_arvalid(m_arvalid[g]),
            .s_axi_arready(m_arready[g]),
            .s_axi_rid(m_rid[g*IDW +: IDW]), .s_axi_rdata(m_rdata[g*DW +: DW]),
            .s_axi_rresp(m_rresp[g*2 +: 2]), .s_axi_rlast(m_rlast[g]),
            .s_axi_rvalid(m_rvalid[g]), .s_axi_rready(m_rready[g])
        );
    end endgenerate

    integer errors = 0, checks = 0;

    task axi_write(input [AW-1:0] a, input [7:0] len, input [DW-1:0] seed);
        integer b;
        begin
            @(negedge clk);
            s_awid = 4'h5; s_awaddr = a; s_awlen = len; s_awsize = 3'd6;
            s_awburst = 2'b01; s_awvalid = 1;
            do @(posedge clk); while (!s_awready);
            @(negedge clk); s_awvalid = 0;
            for (b = 0; b <= len; b = b + 1) begin
                @(negedge clk);
                s_wdata = seed + b; s_wstrb = {STRB{1'b1}};
                s_wlast = (b == len); s_wvalid = 1;
                do @(posedge clk); while (!s_wready);
                @(negedge clk); s_wvalid = 0; s_wlast = 0;
            end
            s_bready = 1;
            do @(posedge clk); while (!s_bvalid);
            if (s_bresp !== 2'b00) begin
                errors = errors + 1; $display("  FAIL bresp=%b addr=%h", s_bresp, a);
            end
            @(negedge clk); s_bready = 0;
        end
    endtask

    task axi_read_check(input [AW-1:0] a, input [7:0] len, input [DW-1:0] seed);
        integer b;
        begin
            @(negedge clk);
            s_arid = 4'h6; s_araddr = a; s_arlen = len; s_arsize = 3'd6;
            s_arburst = 2'b01; s_arvalid = 1;
            do @(posedge clk); while (!s_arready);
            @(negedge clk); s_arvalid = 0;
            s_rready = 1; b = 0;
            forever begin
                do @(posedge clk); while (!s_rvalid);
                checks = checks + 1;
                if (s_rdata !== (seed + b)) begin
                    errors = errors + 1;
                    $display("  FAIL rdata@%h beat%0d got %h exp %h",
                             a, b, s_rdata[63:0], (seed + b));
                end
                if (s_rlast) begin
                    if (b != len) begin errors = errors + 1; $display("  FAIL rlast early"); end
                    @(negedge clk); s_rready = 0; b = 0; disable axi_read_check;
                end
                b = b + 1;
                @(negedge clk);
            end
        end
    endtask

    integer p;
    initial begin
        s_awvalid=0; s_wvalid=0; s_bready=0; s_arvalid=0; s_rready=0; s_wlast=0;
        repeat (8) @(posedge clk); rst = 0; repeat (4) @(posedge clk);

        // distinct single-beat data to each of the 4 ports, then read back.
        for (p = 0; p < N; p = p + 1) begin
            axi_write(p << PSEL_LSB, 8'd0, 512'hA5A5_0000 + (p << 24));
        end
        for (p = 0; p < N; p = p + 1) begin
            axi_read_check(p << PSEL_LSB, 8'd0, 512'hA5A5_0000 + (p << 24));
        end

        // a 4-beat burst to port 2, read back.
        axi_write((2 << PSEL_LSB) | 40'h400, 8'd3, 512'hDEAD_1000);
        axi_read_check((2 << PSEL_LSB) | 40'h400, 8'd3, 512'hDEAD_1000);

        // re-read port 0 to prove the burst to port 2 did not corrupt it.
        axi_read_check(0, 8'd0, 512'hA5A5_0000);

        repeat (8) @(posedge clk);
        if (errors == 0) begin
            $display("  PASS -- deconcentrate 1->%0d, %0d checks, 0 errors", N, checks);
        end
        else begin
            $display("  FAIL -- %0d errors in %0d checks", errors, checks);
        end
        $finish;
    end
endmodule

`default_nettype wire
