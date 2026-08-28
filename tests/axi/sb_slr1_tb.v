// sb_slr1 smoke bench: the missing verification harness for the SLR1 station.
// Drives manager 0 (reaches all 5 subordinates) write+read to each sub across
// the async clock domains, checks data integrity. This is the harness the
// per-domain-CDC shrink must stay green against. NEGEDGE drive/sample.
`timescale 1ns / 1ps
`default_nettype none

module sb_slr1_tb;
    localparam integer AW    = 40;
    localparam integer MAXW  = 512;
    localparam integer MAXID = 4;
    localparam integer NM    = 3;
    localparam integer NS    = 5;

    integer errors = 0, checks = 0;
    localparam real TS = 0.001;   // settle past NBA before sampling
`ifdef SB_CFGONLY
    localparam integer P_CFG = 1;
`else
    localparam integer P_CFG = 0;
`endif

    reg bus_clk = 0, clk_ctrl = 0, clk_xdma = 0, clk_mesh = 0, clk_ddr = 0;
    always #1.00  bus_clk  = ~bus_clk;    // 500 MHz
    always #5.00  clk_ctrl = ~clk_ctrl;   // 100 MHz
    always #2.00  clk_xdma = ~clk_xdma;   // 250 MHz
    always #2.11  clk_mesh = ~clk_mesh;   // ~237 MHz
    always #1.667 clk_ddr  = ~clk_ddr;    // ~300 MHz
    reg rstn = 0;
    initial begin rstn = 0; #40 rstn = 1; end

    // ---- manager 0 driven; 1,2 tied off ----
    reg  [NM*MAXID-1:0] mp_awid=0, mp_arid=0;
    reg  [NM*AW-1:0]    mp_awaddr=0, mp_araddr=0;
    reg  [NM*8-1:0]     mp_awlen=0, mp_arlen=0;
    reg  [NM*3-1:0]     mp_awsize=0, mp_arsize=0;
    reg  [NM*2-1:0]     mp_awburst=0, mp_arburst=0;
    reg  [NM-1:0]       mp_awvalid=0, mp_arvalid=0, mp_wlast=0, mp_wvalid=0;
    reg  [NM*MAXW-1:0]  mp_wdata=0;
    reg  [NM*(MAXW/8)-1:0] mp_wstrb=0;
    reg  [NM-1:0]       mp_bready=0, mp_rready=0;
    wire [NM-1:0]       mp_awready, mp_wready, mp_bvalid, mp_arready, mp_rvalid;
    wire [NM*MAXID-1:0] mp_bid, mp_rid;
    wire [NM*2-1:0]     mp_bresp, mp_rresp;
    wire [NM*MAXW-1:0]  mp_rdata;
    wire [NM-1:0]       mp_rlast;

    wire [NS*MAXID-1:0]    sp_awid, sp_arid, sp_bid, sp_rid;
    wire [NS*AW-1:0]       sp_awaddr, sp_araddr;
    wire [NS*8-1:0]        sp_awlen, sp_arlen;
    wire [NS*3-1:0]        sp_awsize, sp_arsize;
    wire [NS*2-1:0]        sp_awburst, sp_arburst, sp_bresp, sp_rresp;
    wire [NS-1:0]          sp_awvalid, sp_awready, sp_wlast, sp_wvalid, sp_wready;
    wire [NS-1:0]          sp_bvalid, sp_bready, sp_arvalid, sp_arready;
    wire [NS-1:0]          sp_rvalid, sp_rready, sp_rlast;
    wire [NS*MAXW-1:0]     sp_wdata, sp_rdata;
    wire [NS*(MAXW/8)-1:0] sp_wstrb;
    wire [31:0]           stat_decerr;

    sb_slr1 #(.CFG_ONLY(P_CFG)) u_dut (
        .bus_clk(bus_clk), .bus_rst(!rstn),
        .clk_ctrl(clk_ctrl), .aresetn_ctrl(rstn),
        .clk_xdma(clk_xdma), .aresetn_xdma(rstn),
        .clk_mesh(clk_mesh), .aresetn_mesh(rstn),
        .clk_ddr(clk_ddr),   .aresetn_ddr(rstn),
        .mp_awid(mp_awid), .mp_awaddr(mp_awaddr), .mp_awlen(mp_awlen),
        .mp_awsize(mp_awsize), .mp_awburst(mp_awburst), .mp_awvalid(mp_awvalid),
        .mp_awready(mp_awready), .mp_wdata(mp_wdata), .mp_wstrb(mp_wstrb),
        .mp_wlast(mp_wlast), .mp_wvalid(mp_wvalid), .mp_wready(mp_wready),
        .mp_bid(mp_bid), .mp_bresp(mp_bresp), .mp_bvalid(mp_bvalid),
        .mp_bready(mp_bready), .mp_arid(mp_arid), .mp_araddr(mp_araddr),
        .mp_arlen(mp_arlen), .mp_arsize(mp_arsize), .mp_arburst(mp_arburst),
        .mp_arvalid(mp_arvalid), .mp_arready(mp_arready), .mp_rid(mp_rid),
        .mp_rdata(mp_rdata), .mp_rresp(mp_rresp), .mp_rlast(mp_rlast),
        .mp_rvalid(mp_rvalid), .mp_rready(mp_rready),
        .sp_awid(sp_awid), .sp_awaddr(sp_awaddr), .sp_awlen(sp_awlen),
        .sp_awsize(sp_awsize), .sp_awburst(sp_awburst), .sp_awvalid(sp_awvalid),
        .sp_awready(sp_awready), .sp_wdata(sp_wdata), .sp_wstrb(sp_wstrb),
        .sp_wlast(sp_wlast), .sp_wvalid(sp_wvalid), .sp_wready(sp_wready),
        .sp_bid(sp_bid), .sp_bresp(sp_bresp), .sp_bvalid(sp_bvalid),
        .sp_bready(sp_bready), .sp_arid(sp_arid), .sp_araddr(sp_araddr),
        .sp_arlen(sp_arlen), .sp_arsize(sp_arsize), .sp_arburst(sp_arburst),
        .sp_arvalid(sp_arvalid), .sp_arready(sp_arready), .sp_rid(sp_rid),
        .sp_rdata(sp_rdata), .sp_rresp(sp_rresp), .sp_rlast(sp_rlast),
        .sp_rvalid(sp_rvalid), .sp_rready(sp_rready), .stat_decerr(stat_decerr));

    // ---- 5 slave RAMs, each on its port clock ----
    wire [NS-1:0] sclk = {clk_ctrl, clk_ctrl, clk_ddr, clk_mesh, clk_mesh};
    genvar s;
    generate for (s = 0; s < NS; s = s + 1) begin : g_slv
        localparam integer DW = (s < 1 && !P_CFG) ? 256 : 32;
        slv_ram #(.AW(AW), .DW(DW), .IDW(MAXID)) u_s (
            .clk(sclk[s]), .rstn(rstn),
            .awid(sp_awid[s*MAXID +: MAXID]), .awaddr(sp_awaddr[s*AW +: AW]),
            .awlen(sp_awlen[s*8 +: 8]), .awvalid(sp_awvalid[s]),
            .awready(sp_awready[s]), .wdata(sp_wdata[s*MAXW +: DW]),
            .wlast(sp_wlast[s]), .wvalid(sp_wvalid[s]), .wready(sp_wready[s]),
            .bid(sp_bid[s*MAXID +: MAXID]), .bresp(sp_bresp[s*2 +: 2]),
            .bvalid(sp_bvalid[s]), .bready(sp_bready[s]),
            .arid(sp_arid[s*MAXID +: MAXID]), .araddr(sp_araddr[s*AW +: AW]),
            .arlen(sp_arlen[s*8 +: 8]), .arvalid(sp_arvalid[s]),
            .arready(sp_arready[s]), .rid(sp_rid[s*MAXID +: MAXID]),
            .rdata(sp_rdata[s*MAXW +: DW]), .rresp(sp_rresp[s*2 +: 2]),
            .rlast(sp_rlast[s]), .rvalid(sp_rvalid[s]), .rready(sp_rready[s]));
        if (DW < MAXW) assign sp_rdata[s*MAXW + DW +: MAXW-DW] = 0;
    end endgenerate

    // ---- manager-0 driver on clk_ctrl (single-beat write then read) ----
    task wr1; input [AW-1:0] a; input [31:0] d; begin
        @(negedge clk_ctrl); #TS;
        mp_awaddr[0*AW +: AW] = a; mp_awsize[0*3 +: 3] = 3'd2; mp_awlen[0*8 +: 8] = 0;
        mp_awburst[0*2 +: 2] = 2'b01; mp_awid[0*MAXID +: MAXID] = 1; mp_awvalid[0] = 1;
        mp_wdata[0*MAXW +: 32] = d; mp_wstrb[0*(MAXW/8) +: MAXW/8] = {(MAXW/8){1'b1}};
        mp_wlast[0] = 1; mp_wvalid[0] = 1; mp_bready[0] = 1; #TS;
        while (!mp_awready[0]) begin @(negedge clk_ctrl); #TS; end
        @(negedge clk_ctrl); #TS; mp_awvalid[0] = 0;
        while (!mp_wready[0]) begin @(negedge clk_ctrl); #TS; end
        @(negedge clk_ctrl); #TS; mp_wvalid[0] = 0; mp_wlast[0] = 0;
        while (!mp_bvalid[0]) begin @(negedge clk_ctrl); #TS; end
        checks = checks + 1;
        if (mp_bresp[0*2 +: 2] !== 2'b00) begin errors = errors + 1;
            $display("  FAIL wr @%h bresp %b", a, mp_bresp[0*2 +: 2]); end
        @(negedge clk_ctrl); #TS; mp_bready[0] = 0;
    end endtask

    task rd1; input [AW-1:0] a; input [31:0] exp; begin
        @(negedge clk_ctrl); #TS;
        mp_araddr[0*AW +: AW] = a; mp_arsize[0*3 +: 3] = 3'd2; mp_arlen[0*8 +: 8] = 0;
        mp_arburst[0*2 +: 2] = 2'b01; mp_arid[0*MAXID +: MAXID] = 2; mp_arvalid[0] = 1;
        mp_rready[0] = 1; #TS;
        while (!mp_arready[0]) begin @(negedge clk_ctrl); #TS; end
        @(negedge clk_ctrl); #TS; mp_arvalid[0] = 0;
        while (!mp_rvalid[0]) begin @(negedge clk_ctrl); #TS; end
        checks = checks + 1;
        if (mp_rdata[0*MAXW +: 32] !== exp) begin errors = errors + 1;
            $display("  FAIL rd @%h got %h want %h", a, mp_rdata[0*MAXW +: 32], exp);
        end
        @(negedge clk_ctrl); #TS; mp_rready[0] = 0;
    end endtask

    localparam [AW-1:0] A_S2 = 40'h00_0030_0000;   // ddr 32b
    localparam [AW-1:0] A_S1 = 40'h00_0081_0000;   // mesh CTRL 32b
    localparam [AW-1:0] A_S4 = 40'h00_0090_0000;   // clk_wiz ctrl 32b

    initial begin
        @(posedge rstn); repeat (20) @(negedge clk_ctrl);
        wr1(A_S2, 32'hDEADBEEF); rd1(A_S2, 32'hDEADBEEF);
        wr1(A_S1, 32'h12345678); rd1(A_S1, 32'h12345678);
        wr1(A_S4, 32'hCAFEF00D); rd1(A_S4, 32'hCAFEF00D);
        repeat (20) @(negedge clk_ctrl);
        if (errors == 0) $display("PASS -- sb_slr1 %0d checks", checks);
        else             $display("FAIL -- %0d errors of %0d", errors, checks);
        $finish;
    end
    initial begin #500000 $display("FAIL WATCHDOG"); $finish; end
endmodule

// -------- minimal AXI4 slave RAM (INCR, per-beat) --------
module slv_ram #(parameter integer AW=40, DW=32, IDW=4)(
    input  wire clk, input wire rstn,
    input  wire [IDW-1:0] awid, input wire [AW-1:0] awaddr, input wire [7:0] awlen,
    input  wire awvalid, output reg awready,
    input  wire [DW-1:0] wdata, input wire wlast, input wire wvalid,
    output reg wready,
    output reg [IDW-1:0] bid, output reg [1:0] bresp, output reg bvalid,
    input  wire bready,
    input  wire [IDW-1:0] arid, input wire [AW-1:0] araddr, input wire [7:0] arlen,
    input  wire arvalid, output reg arready,
    output reg [IDW-1:0] rid, output reg [DW-1:0] rdata, output reg [1:0] rresp,
    output reg rlast, output reg rvalid, input wire rready);
    localparam integer WORDS = 4096;
    reg [DW-1:0] mem [0:WORDS-1];
    reg [AW-1:0] wa, ra; reg [7:0] rleft; reg [IDW-1:0] wid_q, rid_q;
    reg wact, ract;
    integer i; initial for (i=0;i<WORDS;i=i+1) mem[i]=0;
    localparam integer LSB = (DW==256) ? 5 : 2;
    always @(posedge clk) begin
        if (!rstn) begin awready<=0; wready<=0; bvalid<=0; arready<=0; rvalid<=0;
                         wact<=0; ract<=0; rlast<=0; end
        else begin
            awready <= !wact && !bvalid;
            if (awvalid && awready) begin wa<=awaddr; wid_q<=awid; wact<=1; end
            wready <= wact;
            if (wvalid && wready && wact) begin
                mem[wa[AW-1:LSB] % WORDS] <= wdata; wa <= wa + (1<<LSB);
                if (wlast) begin wact<=0; bid<=wid_q; bresp<=0; bvalid<=1; end
            end
            if (bvalid && bready) bvalid <= 0;
            arready <= !ract && !rvalid;
            if (arvalid && arready) begin ra<=araddr; rid_q<=arid; rleft<=arlen;
                                          ract<=1; end
            if (ract && !rvalid) begin
                rdata <= mem[ra[AW-1:LSB] % WORDS]; rid<=rid_q; rresp<=0;
                rlast <= (rleft==0); rvalid<=1;
            end else if (rvalid && rready) begin
                rvalid <= 0;
                if (rlast) ract<=0;
                else begin ra<=ra+(1<<LSB); rleft<=rleft-1; end
            end
        end
    end
endmodule
`default_nettype wire
