// ONE NMU and ONE NSU, one clock, no station, no links: the width conversion
// on its own. Every combination the card has -- 32/64 Lite+full and 512
// managers against 256 full and 32 Lite subordinates -- is a parameter pair,
// so a failure names the RATIO rather than a system symptom.

// -d W_MW=<bits> -d W_SDW=<bits> selects the pair; the defaults are the jtag
// case (64 -> 256) that v6.5 corrupted on hardware.

`timescale 1ns / 1ps
`default_nettype none

module sb_width_tb;
`ifdef W_MW
    localparam integer MW = `W_MW;
`else
    localparam integer MW = 64;
`endif
`ifdef W_SDW
    localparam integer SDW = `W_SDW;
`else
    localparam integer SDW = 256;
`endif
    localparam integer FW   = 256;
    localparam integer AW   = 40;
    localparam integer IDW  = 4;
    localparam integer TAGW = 4;
    localparam integer DSTW = 2;
    localparam integer WORDS = 64;                  // subordinate RAM, in SDW

    localparam integer MB = MW / 8;                 // manager beat, bytes
    localparam integer SB = SDW / 8;                // subordinate beat, bytes
    localparam real    TS = 0.001;

    integer errors = 0, checks = 0;

    reg clk = 0;
    always #2 clk = ~clk;
    reg rstn = 0;

    // ---------------------------------------------------------- the manager
    reg  [IDW-1:0]  awid = 0, arid = 0;
    reg  [AW-1:0]   awaddr = 0, araddr = 0;
    reg  [7:0]      awlen = 0, arlen = 0;
    reg  [2:0]      awsize = 0, arsize = 0;
    reg             awvld = 0, arvld = 0, wvld = 0, wlast = 0;
    reg  [MW-1:0]   wdata = 0;
    reg  [MB-1:0]   wstrb = 0;
    reg             brdy = 1, rrdy = 1;
    wire            awrdy, wrdy, arrdy, bvld, rvld, rlast;
    wire [1:0]      bresp, rresp;
    wire [MW-1:0]   rdata;

    // ------------------------------------------------------------- the flit
    wire            rq_v, rq_r, rq_wr, rq_head, rq_last;
    wire [DSTW-1:0] rq_dst, rq_dpt;
    wire [TAGW-1:0] rq_tag;
    wire [AW-1:0]   rq_addr;
    wire [7:0]      rq_len;
    wire [2:0]      rq_size;
    wire [FW-1:0]   rq_data;
    wire [FW/8-1:0] rq_strb;

    wire            rs_v, rs_r, rs_wr, rs_last;
    wire [DSTW-1:0] rs_dst;
    wire [TAGW-1:0] rs_tag;
    wire [1:0]      rs_resp;
    wire [FW-1:0]   rs_data;

    // Every address is this port's; one segment, identity translation.
    sb_nmu #(.MW(MW), .MIDW(IDW), .AW(AW), .FW(FW), .TAGW(TAGW), .DSTW(DSTW),
             .NSEG(1), .REQ_DEPTH(64), .RSP_DEPTH(64), .MAX_BURST(0),
             .SEG_BASE({AW{1'b0}}), .SEG_MASK({AW{1'b0}}),
             .SEG_XLT({AW{1'b0}}), .SEG_DST({DSTW{1'b0}}),
             .SEG_DPORT({DSTW{1'b0}}), .SEG_VLD(1'b1)) u_nmu (
        .s_aclk(clk), .s_aresetn(rstn),
        .s_awid(awid), .s_awaddr(awaddr), .s_awlen(awlen), .s_awsize(awsize),
        .s_awburst(2'b01), .s_awvalid(awvld), .s_awready(awrdy),
        .s_wdata(wdata), .s_wstrb(wstrb), .s_wlast(wlast), .s_wvalid(wvld),
        .s_wready(wrdy),
        .s_bid(), .s_bresp(bresp), .s_bvalid(bvld), .s_bready(brdy),
        .s_arid(arid), .s_araddr(araddr), .s_arlen(arlen), .s_arsize(arsize),
        .s_arburst(2'b01), .s_arvalid(arvld), .s_arready(arrdy),
        .s_rid(), .s_rdata(rdata), .s_rresp(rresp), .s_rlast(rlast),
        .s_rvalid(rvld), .s_rready(rrdy),
        .bus_clk(clk), .bus_rst(!rstn),
        .req_valid(rq_v), .req_ready(rq_r), .req_dst(rq_dst),
        .req_dport(rq_dpt), .req_tag(rq_tag), .req_wr(rq_wr),
        .req_head(rq_head), .req_last(rq_last), .req_addr(rq_addr),
        .req_len(rq_len), .req_size(rq_size), .req_data(rq_data),
        .req_strb(rq_strb),
        .rsp_valid(rs_v), .rsp_ready(rs_r), .rsp_tag(rs_tag), .rsp_wr(rs_wr),
        .rsp_last(rs_last), .rsp_resp(rs_resp), .rsp_data(rs_data),
        .stat_decerr()
    );

    wire [IDW-1:0]  m_awid, m_arid;
    wire [AW-1:0]   m_awaddr, m_araddr;
    wire [7:0]      m_awlen, m_arlen;
    wire [2:0]      m_awsize, m_arsize;
    wire [1:0]      m_awburst, m_arburst;
    wire            m_awvalid, m_awready, m_arvalid, m_arready;
    wire [SDW-1:0]  m_wdata, m_rdata;
    wire [SB-1:0]   m_wstrb;
    wire            m_wlast, m_wvalid, m_wready;
    wire            m_bvalid, m_bready, m_rlast, m_rvalid, m_rready;
    wire [1:0]      m_bresp, m_rresp;
    wire [IDW-1:0]  m_bid, m_rid;

    sb_nsu #(.SDW(SDW), .SIDW(IDW), .AW(AW), .FW(FW), .TAGW(TAGW),
             .SRCW(DSTW), .WOST(4), .ROST(4)) u_nsu (
        .bus_clk(clk), .bus_rst(!rstn),
        .req_valid(rq_v), .req_ready(rq_r), .req_src(rq_dst), .req_tag(rq_tag),
        .req_wr(rq_wr), .req_head(rq_head), .req_last(rq_last),
        .req_addr(rq_addr), .req_len(rq_len), .req_size(rq_size),
        .req_data(rq_data), .req_strb(rq_strb),
        .rsp_valid(rs_v), .rsp_ready(rs_r), .rsp_dst(rs_dst), .rsp_tag(rs_tag),
        .rsp_wr(rs_wr), .rsp_last(rs_last), .rsp_resp(rs_resp),
        .rsp_data(rs_data),
        .m_aclk(clk), .m_aresetn(rstn),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen),
        .m_awsize(m_awsize), .m_awburst(m_awburst), .m_awvalid(m_awvalid),
        .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid),
        .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen),
        .m_arsize(m_arsize), .m_arburst(m_arburst), .m_arvalid(m_arvalid),
        .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp),
        .m_rlast(m_rlast), .m_rvalid(m_rvalid), .m_rready(m_rready)
    );

    axi4_ram #(.DATA_WIDTH(SDW), .ADDR_WIDTH(AW), .ID_WIDTH(IDW),
               .DEPTH(WORDS)) u_ram (
        .clk(clk), .resetn(rstn),
        .s_axi_awid(m_awid), .s_axi_awaddr(m_awaddr), .s_axi_awlen(m_awlen),
        .s_axi_awsize(m_awsize), .s_axi_awburst(m_awburst),
        .s_axi_awvalid(m_awvalid), .s_axi_awready(m_awready),
        .s_axi_wdata(m_wdata), .s_axi_wstrb(m_wstrb), .s_axi_wlast(m_wlast),
        .s_axi_wvalid(m_wvalid), .s_axi_wready(m_wready),
        .s_axi_bid(m_bid), .s_axi_bresp(m_bresp), .s_axi_bvalid(m_bvalid),
        .s_axi_bready(m_bready),
        .s_axi_arid(m_arid), .s_axi_araddr(m_araddr), .s_axi_arlen(m_arlen),
        .s_axi_arsize(m_arsize), .s_axi_arburst(m_arburst),
        .s_axi_arvalid(m_arvalid), .s_axi_arready(m_arready),
        .s_axi_rid(m_rid), .s_axi_rdata(m_rdata), .s_axi_rresp(m_rresp),
        .s_axi_rlast(m_rlast), .s_axi_rvalid(m_rvalid), .s_axi_rready(m_rready)
    );

    // ------------------------------------------------------------ stimulus
    // The value a byte at address `a` should hold: address-derived, so a beat
    // landing at the wrong offset is caught rather than looking plausible.
    function [7:0] bpat;
        input [AW-1:0] a;
        begin bpat = a[7:0] ^ {a[11:8], 4'hA}; end
    endfunction

    integer spin;

    task tick;
        begin @(negedge clk); #TS; end
    endtask

    // Every task spins against a CEILING: a stalled handshake ends the bench
    // with a message instead of running the simulator forever.
    task wr;
        input [AW-1:0] a;
        input [7:0]    len;
        input [2:0]    sz;
        integer b, k;
        reg [AW-1:0] ba;
        begin
            tick;
            awid = 4'd1; awaddr = a; awlen = len; awsize = sz; awvld = 1'b1;
            // SETTLE before sampling: ready is combinational on valid, and
            // reading it in the same delta returns the PREVIOUS cycle's value.
            #TS;
            spin = 0;
            while (!awrdy && spin < 2000) begin tick; spin = spin + 1; end
            if (spin >= 2000) begin
                errors = errors + 1;
                $display("  FAIL wr @%h len %0d: AW never accepted", a, len);
                disable wr;
            end
            tick; awvld = 1'b0;
            for (b = 0; b <= len; b = b + 1) begin
                ba = a + b * (1 << sz);
                wdata = {MW{1'b0}}; wstrb = {MB{1'b0}};
                for (k = 0; k < (1 << sz); k = k + 1) begin
                    wdata[((ba[$clog2(MB)-1:0] + k) % MB)*8 +: 8] = bpat(ba + k);
                    wstrb[(ba[$clog2(MB)-1:0] + k) % MB] = 1'b1;
                end
                wlast = (b == len); wvld = 1'b1;
                #TS;
                spin = 0;
                while (!wrdy && spin < 2000) begin tick; spin = spin + 1; end
                if (spin >= 2000) begin
                    errors = errors + 1;
                    $display("  FAIL wr @%h beat %0d: W never accepted", a, b);
                    disable wr;
                end
                tick; wvld = 1'b0; wlast = 1'b0;
            end
            spin = 0;
            while (!bvld && spin < 4000) begin tick; spin = spin + 1; end
            checks = checks + 1;
            if (spin >= 4000) begin
                errors = errors + 1;
                $display("  FAIL wr @%h len %0d: no B response", a, len);
            end else if (bresp !== 2'b00) begin
                errors = errors + 1;
                $display("  FAIL wr @%h bresp %b", a, bresp);
            end
            tick;
        end
    endtask

    task rd;
        input [AW-1:0] a;
        input [7:0]    len;
        input [2:0]    sz;
        integer b, k;
        reg [AW-1:0] ba;
        reg [7:0]    got, exp;
        begin
            tick;
            arid = 4'd2; araddr = a; arlen = len; arsize = sz; arvld = 1'b1;
            #TS;
            spin = 0;
            while (!arrdy && spin < 2000) begin tick; spin = spin + 1; end
            if (spin >= 2000) begin
                errors = errors + 1;
                $display("  FAIL rd @%h len %0d: AR never accepted", a, len);
                disable rd;
            end
            tick; arvld = 1'b0;
            b = 0; spin = 0;
            while (b <= len && spin < 8000) begin
                if (rvld) begin
                    ba = a + b * (1 << sz);
                    checks = checks + 1;
                    for (k = 0; k < (1 << sz); k = k + 1) begin
                        got = rdata[((ba[$clog2(MB)-1:0] + k) % MB)*8 +: 8];
                        exp = bpat(ba + k);
                        if (got !== exp) begin
                            errors = errors + 1;
                            if (errors < 12)
                                $display("  FAIL rd @%h beat %0d byte %0d: got %h exp %h",
                                         a, b, k, got, exp);
                        end
                    end
                    if ((b == len) !== rlast) begin
                        errors = errors + 1;
                        $display("  FAIL rd @%h rlast at beat %0d of %0d",
                                 a, b, len);
                    end
                    b = b + 1;
                end
                spin = spin + 1;
                tick;
            end
            if (b <= len) begin
                errors = errors + 1;
                $display("  FAIL rd @%h stalled at beat %0d of %0d", a, b, len);
            end
        end
    endtask

    localparam [2:0] MSZ = $clog2(MB);

    // The supported contract: a manager WIDER than the flit splits, and its
    // beats must be full-width (the XDMA data engine's only shape); such a
    // manager reaches flit-width subordinates only. Sub-width and narrow
    // slaves belong to the <=FW masters, which convert fully.
    localparam integer SUPP_SUB = (MW <= FW) ? 1 : 0;

    initial begin
        $display("--- width TB: manager %0d-bit, flit %0d-bit, subordinate %0d-bit",
                 MW, FW, SDW);
        if ((MW > FW) && (SDW < FW)) begin
            $display("PASS  0 checks: config unsupported by contract (split manager to sub-flit subordinate)");
            $finish;
        end
        repeat (8) tick;
        rstn = 1;
        repeat (8) tick;

        // 1. one full-width beat, aligned: the simplest thing that must work.
        wr(40'h100, 8'd0, MSZ);
        rd(40'h100, 8'd0, MSZ);

        // 2. a burst of full-width beats, crossing flit boundaries either way.
        wr(40'h200, 8'd7, MSZ);
        rd(40'h200, 8'd7, MSZ);

        // 3. SUB-WIDTH beats: a 32-bit access on a wider port, which is what
        //    every control register op is.
        if (MB > 4 && SUPP_SUB != 0) begin
            wr(40'h300, 8'd0, 3'd2);
            rd(40'h300, 8'd0, 3'd2);
            wr(40'h304, 8'd0, 3'd2);     // the ODD lane, the one that aliased
            rd(40'h304, 8'd0, 3'd2);
            wr(40'h380, 8'd3, 3'd2);     // a sub-width BURST
            rd(40'h380, 8'd3, 3'd2);
        end

        // 4. unaligned start inside a flit, so the head offset is exercised.
        wr(40'h400 + MB, 8'd2, MSZ);
        rd(40'h400 + MB, 8'd2, MSZ);

        if (errors) $display("FAIL  %0d errors in %0d checks", errors, checks);
        else        $display("PASS  %0d checks", checks);
        $finish;
    end

    initial begin
        #2_000_000;
        $display("FAIL  watchdog: width TB did not finish");
        $finish;
    end
endmodule

`default_nettype wire
