// One cell of the 12-permutation conversion matrix: {full,lite} manager x
// {full,lite} subordinate x {MW>SDW, MW=SDW, MW<SDW}, selected by defines:
//   -d MGR_IS_LITE     (absent = full AXI4 manager)
//   -d SUB_IS_LITE     (absent = full AXI4 subordinate)
//   -d MW64 / -d SW64  (width flags; absent = 32-bit. xvlog.bat mangles
//                       NAME=VALUE defines, so flags only)
// The DUT is the production NMU -> flit contract -> NSU pair at FW=256, the
// exact component pair every station port instantiates; the hub between them
// only routes. Endpoints are verified by backdoor, hangs are bounded, and a
// Lite endpoint that IGNORES WSTRB (which a Lite slave may) is the default.

`timescale 1ns / 1ps
`default_nettype none

module sb_conv12_tb;

    localparam integer AW   = 43;
    localparam integer FW   = 256;
`ifdef MW1024
    localparam integer MW   = 1024;
`elsif MW512
    localparam integer MW   = 512;
`elsif MW256
    localparam integer MW   = 256;
`elsif MW128
    localparam integer MW   = 128;
`elsif MW64
    localparam integer MW   = 64;
`else
    localparam integer MW   = 32;
`endif
`ifdef SW512
    localparam integer SDW  = 512;
`elsif SW256
    localparam integer SDW  = 256;
`elsif SW128
    localparam integer SDW  = 128;
`elsif SW64
    localparam integer SDW  = 64;
`else
    localparam integer SDW  = 32;
`endif
    localparam integer SZM  = $clog2(MW/8);
    localparam integer GW   = (MW < 64) ? MW : 64;
    localparam integer W32R = SDW/32;
`ifdef SUB_IS_LITE
`ifdef SUB_HONORS_WSTRB
    localparam integer LITE_CLOBBER = 0;
`else
    localparam integer LITE_CLOBBER = (SDW == 64) ? 1 : 0;
`endif
`else
    localparam integer LITE_CLOBBER = 0;
`endif
    localparam integer TAGW = 4;
    localparam integer TMO  = 3000;

    localparam [AW-1:0] BASE = 43'h900000;
    localparam [AW-1:0] FULLM = {AW{1'b1}};

    integer data_err = 0;
    integer hangs    = 0;
    integer orphans  = 0;
    integer checks   = 0;

    reg s_aclk = 0, bus_clk = 0, m_aclk = 0;
    always begin
        #5.000 s_aclk  = ~s_aclk;
    end
    always begin
        #2.500 bus_clk = ~bus_clk;
    end
    always begin
        #1.667 m_aclk  = ~m_aclk;
    end

    reg rstn = 0;
    wire bus_rst = !rstn;

    // ------------------------------------------------------------ manager pins
    reg  [3:0]      m_awid   = 0;
    reg  [AW-1:0]   m_awaddr = 0;
    reg  [7:0]      m_awlen  = 0;
    reg  [2:0]      m_awsize = 0;
    reg             m_awvld  = 0;
    reg  [MW-1:0]   m_wdata  = 0;
    reg  [MW/8-1:0] m_wstrb  = 0;
    reg             m_wlast  = 0;
    reg             m_wvld   = 0;
    reg             m_brdy   = 1;
    reg  [3:0]      m_arid   = 0;
    reg  [AW-1:0]   m_araddr = 0;
    reg  [7:0]      m_arlen  = 0;
    reg  [2:0]      m_arsize = 0;
    reg             m_arvld  = 0;
    reg             m_rrdy   = 1;
    // Driven, not tied: WRAP and FIXED are AXI4 and were untestable while the
    // instantiation hardwired INCR.
    reg  [1:0]      m_awburst = 2'b01;
    reg  [1:0]      m_arburst = 2'b01;

    wire            m_awrdy, m_wrdy, m_bvld, m_arrdy, m_rvld, m_rlast;
    wire [1:0]      m_bresp, m_rresp;
    wire [MW-1:0]   m_rdata;

    // ------------------------------------------------------------ flit wires
    wire            rq_v, rq_r, rq_wr, rq_head, rq_last;
    wire [TAGW-1:0] rq_tag;
    wire [AW-1:0]   rq_addr;
    wire [7:0]      rq_len;
    wire [2:0]      rq_size;
    wire [FW-1:0]   rq_data;
    wire [FW/8-1:0] rq_strb;
    wire            rs_v, rs_r, rs_wr, rs_last;
    wire [TAGW-1:0] rs_tag;
    wire [1:0]      rs_resp;
    wire [FW-1:0]   rs_data;

    localparam [AW-1:0] SEG_MASK1 = FULLM ^ 43'hFFFF;

`ifdef MGR_IS_LITE
    sb_nmu_lite #(.MW(MW), .AW(AW), .FW(FW), .TAGW(TAGW), .DSTW(2), .NSEG(1),
                  .REQ_DEPTH(16), .RSP_DEPTH(64),
                  .SEG_BASE(BASE), .SEG_MASK(SEG_MASK1), .SEG_XLT(BASE),
                  .SEG_DST(2'd0), .SEG_VLD(1'b1)) u_nmu (
        .s_aclk(s_aclk), .s_aresetn(rstn),
        .s_awaddr(m_awaddr), .s_awprot(3'b000),
        .s_awvalid(m_awvld), .s_awready(m_awrdy),
        .s_wdata(m_wdata), .s_wstrb(m_wstrb),
        .s_wvalid(m_wvld), .s_wready(m_wrdy),
        .s_bresp(m_bresp), .s_bvalid(m_bvld), .s_bready(m_brdy),
        .s_araddr(m_araddr), .s_arprot(3'b000),
        .s_arvalid(m_arvld), .s_arready(m_arrdy),
        .s_rdata(m_rdata), .s_rresp(m_rresp),
        .s_rvalid(m_rvld), .s_rready(m_rrdy),
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .req_valid(rq_v), .req_ready(rq_r), .req_dst(),
        .req_tag(rq_tag), .req_wr(rq_wr), .req_head(rq_head),
        .req_last(rq_last), .req_addr(rq_addr), .req_len(rq_len),
        .req_size(rq_size), .req_data(rq_data), .req_strb(rq_strb),
        .rsp_valid(rs_v), .rsp_ready(rs_r), .rsp_tag(rs_tag),
        .rsp_wr(rs_wr), .rsp_last(rs_last), .rsp_resp(rs_resp),
        .rsp_data(rs_data),
        .stat_decerr()
    );
    assign m_rlast = 1'b1;
`else
    sb_nmu #(.MW(MW), .MIDW(4), .AW(AW), .FW(FW), .TAGW(TAGW), .DSTW(2),
             .NSEG(1), .REQ_DEPTH(64), .RSP_DEPTH(64),
             .SEG_BASE(BASE), .SEG_MASK(SEG_MASK1), .SEG_XLT(BASE),
             .SEG_DST(2'd0), .SEG_DPORT(2'd0), .SEG_VLD(1'b1)) u_nmu (
        .s_aclk(s_aclk), .s_aresetn(rstn),
        .s_awid(m_awid), .s_awaddr(m_awaddr), .s_awlen(m_awlen),
        .s_awsize(m_awsize), .s_awburst(m_awburst),
        .s_awvalid(m_awvld), .s_awready(m_awrdy),
        .s_wdata(m_wdata), .s_wstrb(m_wstrb), .s_wlast(m_wlast),
        .s_wvalid(m_wvld), .s_wready(m_wrdy),
        .s_bid(), .s_bresp(m_bresp), .s_bvalid(m_bvld), .s_bready(m_brdy),
        .s_arid(m_arid), .s_araddr(m_araddr), .s_arlen(m_arlen),
        .s_arsize(m_arsize), .s_arburst(m_arburst),
        .s_arvalid(m_arvld), .s_arready(m_arrdy),
        .s_rid(), .s_rdata(m_rdata), .s_rresp(m_rresp), .s_rlast(m_rlast),
        .s_rvalid(m_rvld), .s_rready(m_rrdy),
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .req_valid(rq_v), .req_ready(rq_r), .req_dst(), .req_dport(),
        .req_tag(rq_tag), .req_wr(rq_wr), .req_head(rq_head),
        .req_last(rq_last), .req_addr(rq_addr), .req_len(rq_len),
        .req_size(rq_size), .req_data(rq_data), .req_strb(rq_strb),
        .rsp_valid(rs_v), .rsp_ready(rs_r), .rsp_tag(rs_tag),
        .rsp_wr(rs_wr), .rsp_last(rs_last), .rsp_resp(rs_resp),
        .rsp_data(rs_data),
        .stat_decerr()
    );
`endif

    // ------------------------------------------------------------- subordinate
    wire [AW-1:0]    e_awaddr, e_araddr;
    wire             e_awvld, e_awrdy, e_wvld, e_wrdy, e_bvld, e_brdy;
    wire             e_arvld, e_arrdy, e_rvld, e_rrdy;
    wire [SDW-1:0]   e_wdata, e_rdata;
    wire [SDW/8-1:0] e_wstrb;
    wire [1:0]       e_bresp, e_rresp;

`ifdef SUB_IS_LITE
    sb_nsu_lite #(.SDW(SDW), .AW(AW), .FW(FW), .TAGW(TAGW), .SRCW(2),
                  .REQ_DEPTH(16), .RSP_DEPTH(16)) u_conv (
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .req_valid(rq_v), .req_ready(rq_r), .req_src(2'd0),
        .req_tag(rq_tag), .req_wr(rq_wr), .req_head(rq_head),
        .req_last(rq_last), .req_addr(rq_addr), .req_len(rq_len),
        .req_size(rq_size), .req_data(rq_data), .req_strb(rq_strb),
        .rsp_valid(rs_v), .rsp_ready(rs_r), .rsp_dst(),
        .rsp_tag(rs_tag), .rsp_wr(rs_wr), .rsp_last(rs_last),
        .rsp_resp(rs_resp), .rsp_data(rs_data),
        .m_aclk(m_aclk), .m_aresetn(rstn),
        .m_awaddr(e_awaddr), .m_awprot(),
        .m_awvalid(e_awvld), .m_awready(e_awrdy),
        .m_wdata(e_wdata), .m_wstrb(e_wstrb),
        .m_wvalid(e_wvld), .m_wready(e_wrdy),
        .m_bresp(e_bresp), .m_bvalid(e_bvld), .m_bready(e_brdy),
        .m_araddr(e_araddr), .m_arprot(),
        .m_arvalid(e_arvld), .m_arready(e_arrdy),
        .m_rdata(e_rdata), .m_rresp(e_rresp),
        .m_rvalid(e_rvld), .m_rready(e_rrdy)
    );

    alite_ep #(.DW(SDW), .AW(AW),
`ifdef SUB_HONORS_WSTRB
               .IGNORE_WSTRB(0)
`else
               .IGNORE_WSTRB(1)
`endif
    ) u_ep (
        .clk(m_aclk), .resetn(rstn),
        .awaddr(e_awaddr), .awvalid(e_awvld), .awready(e_awrdy),
        .wdata(e_wdata), .wstrb(e_wstrb), .wvalid(e_wvld), .wready(e_wrdy),
        .bresp(e_bresp), .bvalid(e_bvld), .bready(e_brdy),
        .araddr(e_araddr), .arvalid(e_arvld), .arready(e_arrdy),
        .rdata(e_rdata), .rresp(e_rresp), .rvalid(e_rvld), .rready(e_rrdy)
    );
`else
    wire [3:0] e_awid, e_arid, e_bid, e_rid;
    wire [7:0] e_awlen, e_arlen;
    wire [2:0] e_awsize, e_arsize;
    wire [1:0] e_awburst, e_arburst;
    wire       e_wlast, e_rlast;

    sb_nsu #(.SDW(SDW), .SIDW(4), .AW(AW), .FW(FW), .TAGW(TAGW), .SRCW(2),
             .REQ_DEPTH(16), .RSP_DEPTH(16)) u_conv (
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .req_valid(rq_v), .req_ready(rq_r), .req_src(2'd0),
        .req_tag(rq_tag), .req_wr(rq_wr), .req_head(rq_head),
        .req_last(rq_last), .req_addr(rq_addr), .req_len(rq_len),
        .req_size(rq_size), .req_data(rq_data), .req_strb(rq_strb),
        .rsp_valid(rs_v), .rsp_ready(rs_r), .rsp_dst(),
        .rsp_tag(rs_tag), .rsp_wr(rs_wr), .rsp_last(rs_last),
        .rsp_resp(rs_resp), .rsp_data(rs_data),
        .m_aclk(m_aclk), .m_aresetn(rstn),
        .m_awid(e_awid), .m_awaddr(e_awaddr), .m_awlen(e_awlen),
        .m_awsize(e_awsize), .m_awburst(e_awburst),
        .m_awvalid(e_awvld), .m_awready(e_awrdy),
        .m_wdata(e_wdata), .m_wstrb(e_wstrb), .m_wlast(e_wlast),
        .m_wvalid(e_wvld), .m_wready(e_wrdy),
        .m_bid(e_bid), .m_bresp(e_bresp), .m_bvalid(e_bvld),
        .m_bready(e_brdy),
        .m_arid(e_arid), .m_araddr(e_araddr), .m_arlen(e_arlen),
        .m_arsize(e_arsize), .m_arburst(e_arburst),
        .m_arvalid(e_arvld), .m_arready(e_arrdy),
        .m_rid(e_rid), .m_rdata(e_rdata), .m_rresp(e_rresp),
        .m_rlast(e_rlast), .m_rvalid(e_rvld), .m_rready(e_rrdy)
    );

    axi4_ram #(.DATA_WIDTH(SDW), .ADDR_WIDTH(AW), .ID_WIDTH(4),
               .DEPTH(512)) u_ram (
        .clk(m_aclk), .resetn(rstn),
        .s_axi_awid(e_awid), .s_axi_awaddr(e_awaddr), .s_axi_awlen(e_awlen),
        .s_axi_awsize(e_awsize), .s_axi_awburst(e_awburst),
        .s_axi_awvalid(e_awvld), .s_axi_awready(e_awrdy),
        .s_axi_wdata(e_wdata), .s_axi_wstrb(e_wstrb), .s_axi_wlast(e_wlast),
        .s_axi_wvalid(e_wvld), .s_axi_wready(e_wrdy),
        .s_axi_bid(e_bid), .s_axi_bresp(e_bresp),
        .s_axi_bvalid(e_bvld), .s_axi_bready(e_brdy),
        .s_axi_arid(e_arid), .s_axi_araddr(e_araddr), .s_axi_arlen(e_arlen),
        .s_axi_arsize(e_arsize), .s_axi_arburst(e_arburst),
        .s_axi_arvalid(e_arvld), .s_axi_arready(e_arrdy),
        .s_axi_rid(e_rid), .s_axi_rdata(e_rdata), .s_axi_rresp(e_rresp),
        .s_axi_rlast(e_rlast), .s_axi_rvalid(e_rvld), .s_axi_rready(e_rrdy)
    );

    integer pk, pl;
    initial begin
        for (pk = 0; pk < 512; pk = pk + 1) begin
            for (pl = 0; pl < SDW/32; pl = pl + 1) begin
                u_ram.mem[pk][pl*32 +: 32] = 32'hC0DE0000 | (pk*(SDW/32) + pl);
            end
        end
    end
`endif

    // Backdoor: 32-bit word j of the endpoint (byte address j*4 in-block).
    function [31:0] bd32;
        input integer j;
        begin
`ifdef SUB_IS_LITE
            bd32 = u_ep.regs[j / W32R][(j % W32R)*32 +: 32];
`else
            bd32 = u_ram.mem[j / W32R][(j % W32R)*32 +: 32];
`endif
        end
    endfunction

    // Preload pattern: 32-bit word j reads {C0DE, j} at every SDW.
    function [31:0] pre32;
        input integer j;
        begin
            pre32 = {16'hC0DE, j[15:0]};
        end
    endfunction

    function [63:0] exp_rd;
        input integer byteaddr;
        begin
            if (MW >= 64) begin
                exp_rd = {pre32(byteaddr/4 + 1), pre32(byteaddr/4)};
            end
            else begin
                exp_rd = {32'd0, pre32(byteaddr/4)};
            end
        end
    endfunction

    // ------------------------------------------------------------- driver
    wire [MW+63:0] m_rdata_pad = {64'd0, m_rdata};
    reg [63:0] got;
    reg [1:0]  gresp;
    reg        timed_out;

    task drd;
        input [AW-1:0] a;
        integer t;
        begin
            timed_out = 0;
            @(negedge s_aclk);
            m_araddr = a; m_arsize = SZM;
            m_arlen = 0; m_arvld = 1;
            t = 0;
            // Ready here is a combinational accept pulse that dies the moment
            // the transfer lands, so the handshake is only observable AT the
            // posedge; #1 deassert keeps the next edge from double-issuing.
            begin : arwait
                forever begin
                    @(posedge s_aclk); t = t + 1;
                    if (m_arrdy === 1'b1) begin #1 m_arvld = 0; disable arwait; end
                    if (t >= TMO) begin
                        $display("    HANG: AR @%h", a); hangs = hangs + 1;
                        timed_out = 1; m_arvld = 0; disable drd;
                    end
                end
            end
            t = 0;
            while (!(m_rvld === 1'b1) && (t < TMO)) begin @(negedge s_aclk); t = t + 1; end
            if (t >= TMO) begin
                $display("    HANG: R @%h", a); hangs = hangs + 1;
                timed_out = 1; disable drd;
            end
            got   = m_rdata_pad[63:0];
            gresp = m_rresp;
            @(negedge s_aclk);
        end
    endtask

    task dwr;
        input [AW-1:0]    a;
        input [63:0]      d;
        input [7:0]       strb;
        integer t;
        reg awd, wd, aw_hs, w_hs;
        begin
            timed_out = 0;
            @(negedge s_aclk);
            m_awaddr = a; m_awsize = SZM; m_awlen = 0;
            m_awvld = 1;
            m_wdata = d[MW-1:0]; m_wstrb = strb; m_wlast = 1;
            m_wvld = 1;
            awd = 0; wd = 0;
            t = 0;
            while (!(awd && wd) && (t < TMO)) begin
                @(posedge s_aclk);
                // Sample BOTH readies before acting: any delay between the two
                // checks reads post-edge comb state and fakes a handshake that
                // never clocked. NBA deasserts hold valid through this edge.
                aw_hs = m_awvld && (m_awrdy === 1'b1);
                w_hs  = m_wvld && (m_wrdy === 1'b1);
                if (aw_hs) begin m_awvld <= 1'b0; awd = 1; end
                if (w_hs)  begin m_wvld  <= 1'b0; wd  = 1; end
                t = t + 1;
            end
            if (t >= TMO) begin
                $display("    HANG: AW/W @%h aw=%b w=%b", a, awd, wd);
                hangs = hangs + 1; timed_out = 1;
                m_awvld = 0; m_wvld = 0; disable dwr;
            end
            t = 0;
            while (!(m_bvld === 1'b1) && (t < TMO)) begin @(negedge s_aclk); t = t + 1; end
            if (t >= TMO) begin
                $display("    HANG: B @%h", a); hangs = hangs + 1;
                dump_state;
                timed_out = 1; disable dwr;
            end
            gresp = m_bresp;
            @(negedge s_aclk);
        end
    endtask

    task dwr_burst4;
        input [AW-1:0] a;
        input [63:0]   d0;
        integer t, beat;
        reg awd;
        begin
            timed_out = 0;
            @(negedge s_aclk);
            m_awaddr = a; m_awsize = SZM; m_awlen = 3;
            m_awvld = 1;
            awd = 0; t = 0;
            while (!awd && (t < TMO)) begin
                @(posedge s_aclk);
                if (m_awrdy === 1'b1) begin #1 m_awvld = 0; awd = 1; end
                t = t + 1;
            end
            if (t >= TMO) begin
                $display("    HANG: burst AW @%h", a); hangs = hangs + 1;
                timed_out = 1; m_awvld = 0; disable dwr_burst4;
            end
            for (beat = 0; beat < 4; beat = beat + 1) begin
                m_wdata = (d0 + beat); m_wstrb = {MW/8{1'b1}};
                m_wlast = (beat == 3); m_wvld = 1;
                t = 0;
                begin : wwait
                    forever begin
                        @(posedge s_aclk); t = t + 1;
                        if (m_wvld && (m_wrdy === 1'b1)) begin
                            #1 m_wvld = 0;
                            disable wwait;
                        end
                        if (t >= TMO) begin
                            $display("    HANG: burst W beat %0d @%h", beat, a);
                            hangs = hangs + 1; timed_out = 1; m_wvld = 0;
                            disable dwr_burst4;
                        end
                    end
                end
            end
            m_wvld = 0; m_wlast = 0;
            t = 0;
            while (!(m_bvld === 1'b1) && (t < TMO)) begin @(negedge s_aclk); t = t + 1; end
            if (t >= TMO) begin
                $display("    HANG: burst B @%h", a); hangs = hangs + 1;
                timed_out = 1; disable dwr_burst4;
            end
            gresp = m_bresp;
            @(negedge s_aclk);
        end
    endtask

    task settle;
        begin repeat (400) @(negedge s_aclk); end
    endtask

`ifdef MGR_IS_LITE
    `define NMUP u_nmu.u_nmu
`else
    `define NMUP u_nmu
`endif
`ifdef SUB_IS_LITE
    `define NSUP u_conv.u_nsu
`else
    `define NSUP u_conv
`endif

    task dump_state;
        begin
            $display("    ---- STATE ----");
            $display("    NMU: wst=%0d tagb=%b avail=%b cred=%0d reqf e/f=%b/%b tokf e/f=%b/%b send=%b rqv/r=%b/%b rspf e=%b",
                     `NMUP.wst, `NMUP.tag_busy, `NMUP.tag_avail,
                     `NMUP.rsp_credit, `NMUP.reqf_empty, `NMUP.reqf_full,
                     `NMUP.tokf_empty, `NMUP.tokf_full, `NMUP.sending,
                     `NMUP.req_valid, `NMUP.req_ready, `NMUP.rspf_empty);
            $display("    NSU: rq e/f=%b/%b body=%b sw=%b bw=%b wbusy=%b awq_e=%b wq_e=%b arq_e=%b | awv/r=%b/%b wv/r=%b/%b bv/r=%b/%b arv/r=%b/%b rv=%b | rsf e/f=%b/%b rspv/r=%b/%b",
                     `NSUP.rq_empty, `NSUP.rqf_full, `NSUP.in_body,
                     `NSUP.start_wr, `NSUP.body_wr, `NSUP.w_busy,
                     `NSUP.awq_empty, `NSUP.wq_empty, `NSUP.arq_empty,
                     `NSUP.m_awvalid, `NSUP.m_awready,
                     `NSUP.m_wvalid, `NSUP.m_wready,
                     `NSUP.m_bvalid, `NSUP.m_bready,
                     `NSUP.m_arvalid, `NSUP.m_arready, `NSUP.m_rvalid,
                     `NSUP.rsf_empty, `NSUP.rsf_full,
                     `NSUP.rsp_valid, `NSUP.rsp_ready);
            $display("    ---------------");
        end
    endtask

    task ckd;
        input [8*32-1:0] name;
        input [63:0]     want;
        begin
            checks = checks + 1;
            if (timed_out) begin
                data_err = data_err + 1;
                $display("    CHECK %0s: TIMEOUT", name);
            end else if (got !== want) begin
                data_err = data_err + 1;
                $display("    CHECK %0s: got %h want %h  DATA-ERROR", name, got, want);
            end
            else begin
                $display("    CHECK %0s: ok", name);
            end
        end
    endtask

    task ckr;
        input [8*32-1:0] name;
        input integer    j;
        input [31:0]     want;
        begin
            checks = checks + 1;
            if (bd32(j) !== want) begin
                data_err = data_err + 1;
                $display("    CHECK %0s: word[%0d]=%h want %h  DATA-ERROR",
                         name, j, bd32(j), want);
            end
            else begin
                $display("    CHECK %0s: ok", name);
            end
        end
    endtask

    task orphan_chk;
        begin
`ifdef SUB_IS_LITE
            if (e_wvld) begin
                orphans = orphans + 1;
                $display("    VIOLATION: orphan W beat parked at the Lite endpoint");
            end
`endif
        end
    endtask

    initial begin
        #1800000;
        // Counted, so the verdict says FAIL: checks never run are not passes.
        $display("WATCHDOG sb_conv12_tb: bench did not finish");
        hangs = hangs + 1;
        report;
        $finish;
    end

    task report;
        begin
            $display("CASE mgr=%0s sub=%0s MW=%0d SDW=%0d : %0s  (checks=%0d data_err=%0d hangs=%0d orphans=%0d)",
`ifdef MGR_IS_LITE
                     "lite",
`else
                     "full",
`endif
`ifdef SUB_IS_LITE
                     "lite",
`else
                     "full",
`endif
                     MW, SDW,
                     (data_err == 0 && hangs == 0 && orphans == 0)
                       ? "CASE-PASS" : "CASE-FAIL",
                     checks, data_err, hangs, orphans);
            // xsim.py both filters and matches on a leading PASS/FAIL, so
            // "CASE-PASS" made every cell of this matrix exit 1 however it ran.
            if (data_err == 0 && hangs == 0 && orphans == 0) begin
                $display("PASS sb_conv12_tb: MW=%0d SDW=%0d, %0d checks",
                         MW, SDW, checks);
            end
            else begin
                $display("FAIL sb_conv12_tb: MW=%0d SDW=%0d, %0d checks, %0d data_err, %0d hangs, %0d orphans",
                         MW, SDW, checks, data_err, hangs, orphans);
            end
        end
    endtask

    initial begin
        repeat (40) @(posedge s_aclk);
        rstn = 1;
        repeat (200) @(posedge s_aclk);

        $display("--- reads ---");
        if (MW <= 64) begin
            drd(BASE + 43'h08); ckd("rd +0x08", exp_rd(8));
        end
        drd(BASE + 43'h00); ckd("rd +0x00", exp_rd(0));
        drd(BASE + 43'h20); ckd("rd +0x20", exp_rd(32));

        if (MW <= 64) begin
            $display("--- write +0x08, footprint, readback ---");
            dwr(BASE + 43'h08, 64'hB1B1B1B1_A0A0A0A0, 8'hFF);
            settle;
            ckr("wr word2", 2, 32'hA0A0A0A0);
            // On a WSTRB-ignoring Lite endpoint wider than the write, the
            // co-resident word is clobbered BY THE ENDPOINT, not the fabric:
            // the converter has no narrower write to issue.
            if (MW == 64) begin
                ckr("wr word3", 3, 32'hB1B1B1B1);
            end
            else if (LITE_CLOBBER) begin
                ckr("wr word3 clob", 3, 32'h0);
            end
            else begin
                ckr("wr word3 intact", 3, pre32(3));
            end
            ckr("word0 intact", 0, pre32(0));
            ckr("word1 intact", 1, pre32(1));
            ckr("word4 intact", 4, pre32(4));
            orphan_chk;
            drd(BASE + 43'h08);
            ckd("rb +0x08", (MW == 64) ? 64'hB1B1B1B1_A0A0A0A0
                                       : {32'd0, 32'hA0A0A0A0});

            $display("--- write #2 +0x18 (stale-pairing drift) ---");
            dwr(BASE + 43'h18, 64'hD3D3D3D3_C2C2C2C2, 8'hFF);
            settle;
            ckr("wr2 word6", 6, 32'hC2C2C2C2);
            if (MW == 64) begin
                ckr("wr2 word7", 7, 32'hD3D3D3D3);
            end
            ckr("word5 intact", 5, pre32(5));
            orphan_chk;
        end

`ifndef MGR_IS_LITE
        $display("--- 4-beat burst write + readback @+0x40 ---");
        dwr_burst4(BASE + 43'h40, 64'hE000_0000_0000_0000);
        settle;
        ckr("burst b0", 16, 32'h00000000);
        ckr("burst b1", 16 + MW/32, 32'h00000001);
        orphan_chk;
        drd(BASE + 43'h40);
        ckd("burst rb0", (MW >= 64) ? 64'hE000_0000_0000_0000 : 64'h0);

        if (MW == 64) begin
            $display("--- half-strobe 64b write @+0x10 (low word only) ---");
            dwr(BASE + 43'h10, 64'hFFFF_FFFF_1234_5678, 8'h0F);
            settle;
            ckr("half word4", 4, 32'h12345678);
            // MEASURED: an unstrobed lane arrives as ZERO, so a WSTRB-blind
            // subordinate destroys the co-resident word. The fabric cannot stop it.
            if (LITE_CLOBBER) begin
                ckr("half word5 CLOBBERED", 5, 32'h0);
            end
            else begin
                ckr("half word5 INTACT", 5, pre32(5));
            end
            orphan_chk;
        end
`endif

        report;
        $finish;
    end


endmodule


// Strict AXI4-Lite endpoint; IGNORE_WSTRB=1 models the register-block right
// to apply every byte lane.
module alite_ep #(
    parameter integer DW = 32,
    parameter integer AW = 43,
    parameter integer IGNORE_WSTRB = 1
)(
    input  wire            clk,
    input  wire            resetn,
    input  wire [AW-1:0]   awaddr,
    input  wire            awvalid,
    output wire            awready,
    input  wire [DW-1:0]   wdata,
    input  wire [DW/8-1:0] wstrb,
    input  wire            wvalid,
    output wire            wready,
    output wire [1:0]      bresp,
    output reg             bvalid,
    input  wire            bready,
    input  wire [AW-1:0]   araddr,
    input  wire            arvalid,
    output wire            arready,
    output reg  [DW-1:0]   rdata,
    output wire [1:0]      rresp,
    output reg             rvalid,
    input  wire            rready
);
    localparam integer N   = 256;
    localparam integer LSB = (DW == 64) ? 3 : 2;
    reg [DW-1:0] regs [0:N-1];
    integer k, l;
    initial for (k = 0; k < N; k = k + 1)
        for (l = 0; l < DW/32; l = l + 1) begin
            regs[k][l*32 +: 32] = 32'hC0DE0000 | (k*(DW/32) + l);
        end

    wire [7:0] wi = awaddr[LSB +: 8];
    wire [7:0] ri = araddr[LSB +: 8];

    wire wgo = awvalid && wvalid && (!bvalid || bready);
    assign awready = wgo;
    assign wready  = wgo;
    assign bresp   = 2'b00;

    wire rgo = arvalid && (!rvalid || rready);
    assign arready = rgo;
    assign rresp   = 2'b00;

    integer b;
    always @(posedge clk) begin
        if (!resetn) begin
            bvalid <= 1'b0;
            rvalid <= 1'b0;
        end else begin
            if (bvalid && bready) begin
                bvalid <= 1'b0;
            end
            if (wgo) begin
                $display("%0t     ep WRITE commit addr=%h data=%h strb=%h%s",
                         $time, awaddr[15:0], wdata, wstrb,
                         (IGNORE_WSTRB != 0) ? " (wstrb IGNORED)" : "");
                if (IGNORE_WSTRB != 0) begin
                    regs[wi] <= wdata;
                end
                else
                    for (b = 0; b < DW/8; b = b + 1) begin
                        if (wstrb[b]) begin
                            regs[wi][b*8 +: 8] <= wdata[b*8 +: 8];
                        end
                    end
                bvalid <= 1'b1;
            end
            if (rvalid && rready) begin
                rvalid <= 1'b0;
            end
            if (rgo) begin
                rdata  <= regs[ri];
                rvalid <= 1'b1;
            end
        end
    end
endmodule

`default_nettype wire
