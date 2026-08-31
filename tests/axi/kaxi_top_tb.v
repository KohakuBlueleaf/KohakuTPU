// KohakuAXI full-system end-to-end: master -> xbar -> per-home L3 -> MIG.
// One master, N homes, each home an axi4_ram. Write/read across both homes,
// through the cache, verifying data integrity end to end (hit + miss+fetch).
// Home = addr[HOME_LSB]; each axi4_ram indexes by the low address bits, so the
// home bit is naturally ignored at the MIG (separate memories per home).

`default_nettype none
`timescale 1ns/1ps

`ifndef NHOME
`define NHOME 2
`endif
`ifndef NTXN
`define NTXN 40
`endif

module kaxi_top_tb;
    localparam integer M        = 1;
    localparam integer N_HOME   = `NHOME;
    localparam integer ADDR_W   = 40;
    localparam integer DATA_W   = 256;
    localparam integer ID_W     = 4;
    localparam integer HOME_LSB = 32;
    localparam integer LSB      = 5;
    localparam integer HIDX_W   = (N_HOME <= 1) ? 1 : $clog2(N_HOME);
    localparam integer MIDX_W   = 1;
    localparam integer SID_W    = ID_W + MIDX_W;
    localparam integer NTXN     = `NTXN;

    reg clk = 0; always #2 clk = ~clk;
    reg resetn = 0;

    reg  [ID_W-1:0]   s_awid, s_arid;
    reg  [ADDR_W-1:0] s_awaddr, s_araddr;
    reg  [7:0]        s_awlen, s_arlen;
    reg  [2:0]        s_awsize, s_arsize;
    reg  [1:0]        s_awburst, s_arburst;
    reg               s_awvalid, s_arvalid, s_wvalid, s_wlast, s_bready, s_rready;
    reg  [DATA_W-1:0] s_wdata;
    reg  [DATA_W/8-1:0] s_wstrb;
    wire              s_awready, s_wready, s_arready;
    wire [ID_W-1:0]   s_bid, s_rid;
    wire [1:0]        s_bresp, s_rresp;
    wire              s_bvalid, s_rvalid, s_rlast;
    wire [DATA_W-1:0] s_rdata;

    wire [N_HOME*SID_W-1:0]     m_awid, m_arid, m_bid, m_rid;
    wire [N_HOME*ADDR_W-1:0]    m_awaddr, m_araddr;
    wire [N_HOME*8-1:0]         m_awlen, m_arlen;
    wire [N_HOME*3-1:0]         m_awsize, m_arsize;
    wire [N_HOME*2-1:0]         m_awburst, m_arburst, m_bresp, m_rresp;
    wire [N_HOME-1:0]           m_awvalid, m_awready, m_wvalid, m_wready, m_wlast;
    wire [N_HOME-1:0]           m_bvalid, m_bready, m_arvalid, m_arready;
    wire [N_HOME-1:0]           m_rvalid, m_rready, m_rlast;
    wire [N_HOME*DATA_W-1:0]    m_wdata, m_rdata;
    wire [N_HOME*(DATA_W/8)-1:0] m_wstrb;

    kaxi_top #(.M(M), .N_HOME(N_HOME), .ADDR_W(ADDR_W), .DATA_W(DATA_W),
               .ID_W(ID_W), .HOME_LSB(HOME_LSB)) dut (
        .clk(clk), .resetn(resetn),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen), .s_awsize(s_awsize),
        .s_awburst(s_awburst), .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast), .s_wvalid(s_wvalid),
        .s_wready(s_wready),
        .s_bid(s_bid), .s_bresp(s_bresp), .s_bvalid(s_bvalid), .s_bready(s_bready),
        .s_arid(s_arid), .s_araddr(s_araddr), .s_arlen(s_arlen), .s_arsize(s_arsize),
        .s_arburst(s_arburst), .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(s_rid), .s_rdata(s_rdata), .s_rresp(s_rresp), .s_rlast(s_rlast),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen), .m_awsize(m_awsize),
        .m_awburst(m_awburst), .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast), .m_wvalid(m_wvalid),
        .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid), .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen), .m_arsize(m_arsize),
        .m_arburst(m_arburst), .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_rvalid(m_rvalid), .m_rready(m_rready)
    );

    genvar h;
    generate
    for (h = 0; h < N_HOME; h = h + 1) begin : hmig
        axi4_ram #(.DATA_WIDTH(DATA_W), .ADDR_WIDTH(ADDR_W), .ID_WIDTH(SID_W),
                   .DEPTH(4096)) mig (
            .clk(clk), .resetn(resetn),
            .s_axi_awid(m_awid[h*SID_W +: SID_W]), .s_axi_awaddr(m_awaddr[h*ADDR_W +: ADDR_W]),
            .s_axi_awlen(m_awlen[h*8 +: 8]), .s_axi_awsize(m_awsize[h*3 +: 3]),
            .s_axi_awburst(m_awburst[h*2 +: 2]), .s_axi_awvalid(m_awvalid[h]),
            .s_axi_awready(m_awready[h]),
            .s_axi_wdata(m_wdata[h*DATA_W +: DATA_W]), .s_axi_wstrb(m_wstrb[h*(DATA_W/8) +: DATA_W/8]),
            .s_axi_wlast(m_wlast[h]), .s_axi_wvalid(m_wvalid[h]), .s_axi_wready(m_wready[h]),
            .s_axi_bid(m_bid[h*SID_W +: SID_W]), .s_axi_bresp(m_bresp[h*2 +: 2]),
            .s_axi_bvalid(m_bvalid[h]), .s_axi_bready(m_bready[h]),
            .s_axi_arid(m_arid[h*SID_W +: SID_W]), .s_axi_araddr(m_araddr[h*ADDR_W +: ADDR_W]),
            .s_axi_arlen(m_arlen[h*8 +: 8]), .s_axi_arsize(m_arsize[h*3 +: 3]),
            .s_axi_arburst(m_arburst[h*2 +: 2]), .s_axi_arvalid(m_arvalid[h]),
            .s_axi_arready(m_arready[h]),
            .s_axi_rid(m_rid[h*SID_W +: SID_W]), .s_axi_rdata(m_rdata[h*DATA_W +: DATA_W]),
            .s_axi_rresp(m_rresp[h*2 +: 2]), .s_axi_rlast(m_rlast[h]),
            .s_axi_rvalid(m_rvalid[h]), .s_axi_rready(m_rready[h])
        );
    end
    endgenerate

    integer checks, errors, i, k, wd;

    function [ADDR_W-1:0] adr;
        input integer home;
        input integer word;
        begin adr = (home << HOME_LSB) | (word << LSB); end
    endfunction
    function [DATA_W-1:0] pat;
        input integer home;
        input integer word;
        begin pat = {8{home[3:0], word[23:0], 4'hB}}; end
    endfunction

    task wr;
        input integer home;
        input integer word;
        input integer len;
        begin
            @(posedge clk);
            s_awid <= 4'd1; s_awaddr <= adr(home, word); s_awlen <= len[7:0];
            s_awsize <= LSB[2:0]; s_awburst <= 2'b01; s_awvalid <= 1'b1;
            @(posedge clk); while (!s_awready) @(posedge clk);
            s_awvalid <= 1'b0;
            for (k = 0; k <= len; k = k + 1) begin
                s_wdata <= pat(home, word + k); s_wstrb <= {(DATA_W/8){1'b1}};
                s_wlast <= (k == len); s_wvalid <= 1'b1;
                @(posedge clk); while (!s_wready) @(posedge clk);
                s_wvalid <= 1'b0; s_wlast <= 1'b0;
            end
            s_bready <= 1'b1;
            @(posedge clk); while (!s_bvalid) @(posedge clk);
            s_bready <= 1'b0;
        end
    endtask

    task rd;
        input integer home;
        input integer word;
        input integer len;
        begin
            @(posedge clk);
            s_arid <= 4'd2; s_araddr <= adr(home, word); s_arlen <= len[7:0];
            s_arsize <= LSB[2:0]; s_arburst <= 2'b01; s_arvalid <= 1'b1;
            @(posedge clk); while (!s_arready) @(posedge clk);
            s_arvalid <= 1'b0;
            for (k = 0; k <= len; k = k + 1) begin
                s_rready <= 1'b1;
                @(posedge clk); while (!(s_rvalid && s_rready)) @(posedge clk);
                checks = checks + 1;
                if (s_rdata !== pat(home, word + k)) begin
                    errors = errors + 1;
                    $display("  FAIL h%0d w%0d beat %0d got %h want %h", home,
                             word, k, s_rdata, pat(home, word + k));
                end
                if (s_rlast !== (k == len)) begin
                    errors = errors + 1;
                    $display("  FAIL rlast h%0d w%0d beat %0d", home, word, k);
                end
                s_rready <= 1'b0;
            end
        end
    endtask

    initial begin
        wd = 0;
        forever begin
            @(posedge clk); wd = wd + 1;
            if (wd > 200000) begin $display("  FAIL WATCHDOG"); $finish; end
        end
    end

    initial begin
        s_awvalid=0; s_wvalid=0; s_bready=0; s_arvalid=0; s_rready=0; s_wlast=0;
        checks=0; errors=0;
        resetn=0; repeat (16) @(posedge clk); resetn=1; repeat (8) @(posedge clk);

        // write across both homes, disjoint words; read-back mixes hit + evict
        for (i = 0; i < NTXN; i = i + 1) begin
            wr(i % N_HOME, 100 + i * 20, i % 8);
        end
        for (i = 0; i < NTXN; i = i + 1) begin
            rd(i % N_HOME, 100 + i * 20, i % 8);
        end
        // read again: hot in cache
        for (i = 0; i < NTXN; i = i + 1) begin
            rd(i % N_HOME, 100 + i * 20, i % 8);
        end

        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- %0d homes end-to-end, %0d checks, 0 errors",
                     N_HOME, checks);
        end else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end
endmodule

`default_nettype wire
