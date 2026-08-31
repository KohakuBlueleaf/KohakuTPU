// Read and write throughput of the STOCK mag_dram_port from one requester
// against a fixed-latency DRAM: the ceiling mm_mover_bw_tb is measured against.

// Both clocks are settable. The mesh clock is retunable at runtime on the card
// (clk_wiz_mesh, USE_DYN_RECONFIG); the MIG's ui_clk is not.

`default_nettype none
`timescale 1ns/1ps

`ifndef TB_RD_OUT
  `define TB_RD_OUT 1
`endif
`ifndef TB_RREG
  `define TB_RREG 1
`endif

module mag_dram_port_bw_tb;
    localparam integer N = 5, AWID = 34, SW = 256, MW = 512, IDW = 4;
    localparam integer RD_OUT = `TB_RD_OUT;
    localparam integer REQ = 3, REQ2 = 4;
    localparam integer R = MW / SW;

    real s_half = 5.0, m_half = 1.6667;
    real mesh_mhz = 100.09, dram_ns = 106.7;

    reg s_clk = 0, m_clk = 0, rstn = 0;
    always begin
        #(s_half) s_clk = ~s_clk;
    end
    always begin
        #(m_half) m_clk = ~m_clk;
    end

    reg  [N-1:0]        q_valid, q_write, r_ready;
    reg  [N*AWID-1:0]   q_addr;
    reg  [N*16-1:0]     q_len;
    wire [N-1:0]        q_ready, w_ready, r_valid, r_last, b_valid;
    reg  [N-1:0]        w_valid;
    reg  [N*SW-1:0]     w_data;
    wire [N*SW-1:0]     r_data;

    wire [IDW-1:0]  m_awid, m_arid, m_bid, m_rid;
    wire [AWID-1:0] m_awaddr, m_araddr;
    wire [7:0]      m_awlen, m_arlen;
    wire [2:0]      m_awsize, m_arsize;
    wire [1:0]      m_awburst, m_arburst;
    wire            m_awvalid, m_wvalid, m_wlast, m_arvalid, m_bready, m_rready;
    wire [MW-1:0]   m_wdata;
    wire [MW/8-1:0] m_wstrb;
    reg             m_awready, m_wready, m_arready, m_bvalid, m_rvalid, m_rlast;
    reg  [MW-1:0]   m_rdata;
    reg  [IDW-1:0]  m_rid_r, m_bid_r;

    assign m_bid = m_bid_r;
    assign m_rid = m_rid_r;

    mag_dram_port #(.N(N), .ADDR_W(AWID), .SW(SW), .MW(MW), .ID_W(IDW),
                    .RD_OUT(RD_OUT), .R_REG(`TB_RREG)) dut (
        .s_aclk(s_clk), .s_aresetn(rstn),
        .q_valid(q_valid), .q_ready(q_ready), .q_addr(q_addr),
        .q_len(q_len), .q_write(q_write),
        .w_valid(w_valid), .w_ready(w_ready), .w_data(w_data),
        .w_strb({(N*SW/8){1'b1}}),
        .r_valid(r_valid), .r_ready(r_ready), .r_data(r_data),
        .r_last(r_last), .b_valid(b_valid),
        .m_aclk(m_clk), .m_aresetn(rstn),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen),
        .m_awsize(m_awsize), .m_awburst(m_awburst), .m_awvalid(m_awvalid),
        .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(2'b00), .m_bvalid(m_bvalid), .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen),
        .m_arsize(m_arsize), .m_arburst(m_arburst), .m_arvalid(m_arvalid),
        .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(2'b00), .m_rlast(m_rlast),
        .m_rvalid(m_rvalid), .m_rready(m_rready)
    );

    // ---- DRAM model: in order, LAT ui-cycles, then back to back ----------
    integer LAT = 24;
    integer mcyc = 0;
    // 64 deep AND backpressured. At 16 with arready tied high the queue wrapped
    // once a requester could hold 4 reads, dropping ARs and hanging the sweep.
    reg [7:0]     p_len  [0:63];
    reg [IDW-1:0] p_id   [0:63];
    integer       p_due  [0:63];
    reg [5:0] phead = 0, ptail = 0;
    reg [6:0] pn_q = 0;
    reg [8:0] beats = 0;
    reg       serving = 0;

    always @(posedge m_clk) begin
        if (!rstn) begin
            mcyc <= 0; phead <= 0; ptail <= 0; pn_q <= 0;
            beats <= 0; serving <= 0;
            m_arready <= 1'b1; m_awready <= 1'b1; m_wready <= 1'b1;
            m_rvalid <= 1'b0; m_rlast <= 1'b0; m_bvalid <= 1'b0;
        end else begin
            mcyc <= mcyc + 1;
            m_bvalid <= m_awvalid && m_awready;
            m_bid_r  <= m_awid;
            m_arready <= (pn_q < 7'd60);
            pn_q <= pn_q + ((m_arvalid && m_arready) ? 7'd1 : 7'd0)
                         - ((serving && m_rready && (beats == 9'd0)) ? 7'd1
                                                                    : 7'd0);
            if (m_arvalid && m_arready) begin
                p_len[ptail] <= m_arlen;
                p_id[ptail]  <= m_arid;
                p_due[ptail] <= mcyc + LAT;
                ptail <= ptail + 6'd1;
            end
            if (!serving) begin
                m_rvalid <= 1'b0;
                if ((phead != ptail) && (mcyc >= p_due[phead])) begin
                    serving <= 1'b1;
                    beats   <= {1'b0, p_len[phead]};
                    m_rvalid <= 1'b1;
                    m_rlast  <= (p_len[phead] == 8'd0);
                    m_rid_r  <= p_id[phead];
                    m_rdata  <= {2{mcyc[31:0], 224'd0}};
                end
            end else if (m_rready) begin
                if (beats == 9'd0) begin
                    serving <= 1'b0; m_rvalid <= 1'b0; m_rlast <= 1'b0;
                    phead <= phead + 6'd1;
                end else begin
                    beats <= beats - 9'd1;
                    m_rlast <= (beats == 9'd1);
                    m_rdata <= {2{mcyc[31:0], 224'd0}};
                end
            end
        end
    end

    // ---- the requester under test ---------------------------------------
    integer got, gotw, cyc0, cyc1, cyc1w, scyc, first_word, ar_cyc;
    reg     measuring, seen_first;
    reg [N-1:0] rmask;

    always @(posedge s_clk) begin
        if (!rstn) begin
            scyc <= 0;
        end else begin
            scyc <= scyc + 1;
            if (measuring) begin
                if (|(q_valid & q_ready & rmask) && (ar_cyc < 0)) begin
                    ar_cyc <= scyc;
                end
                if (|(r_valid & r_ready & rmask)) begin
                    got <= got + 1;
                    if (!seen_first) begin
                        first_word <= scyc;
                        seen_first <= 1'b1;
                    end
                    cyc1 <= scyc;
                end
                if (w_valid[REQ2] && w_ready[REQ2]) begin
                    gotw  <= gotw + 1;
                    cyc1w <= scyc;
                end
            end
        end
    end

    integer nb, nb2, i, words, span, errors;
    integer mbs_1, mbs_burst;
    real    wpc, mbs;

    task setclk(input real mhz);
        begin
            s_half   = 500.0 / mhz;
            mesh_mhz = mhz;
            LAT      = $rtoi(dram_ns / (2.0 * m_half));
            repeat (20) @(negedge s_clk);
            $display("--- mesh %0d MHz, ui_clk %0d MHz, DRAM %0d ns = %0d ui-cycles, RD_OUT %0d ---",
                     $rtoi(mhz), $rtoi(500.0 / m_half), $rtoi(dram_ns), LAT, RD_OUT);
        end
    endtask

    task report(input integer n, input integer bursts, input integer lat,
                input [63:0] tag);
        begin
            span = cyc1 - cyc0;
            wpc  = words * 1.0 / span;
            mbs  = wpc * 32.0 * mesh_mhz;
            $display("  %0s %0dMHz n=%0d LAT=%0d | %0d words in %0d cyc = %0d.%03d w/cyc = %0d MB/s | AR->first word %0d cyc",
                     tag, $rtoi(mesh_mhz), n, lat, words, span,
                     $rtoi(wpc), $rtoi((wpc - $rtoi(wpc)) * 1000.0),
                     $rtoi(mbs), first_word - ar_cyc);
        end
    endtask

    // `kk` requester slots at once, so kk reads are in flight on the shared
    // return path: the load P3 would put on it from one requester.
    integer sk, done_b;
    reg [7:0] issued [0:7];
    task sweepk(input integer n, input integer bursts, input integer lat,
                input integer kk);
        begin
            LAT = lat;
            @(negedge s_clk);
            got = 0; measuring = 1'b1; seen_first = 1'b0;
            ar_cyc = -1; first_word = 0; cyc1 = 0;
            cyc0 = scyc;
            rmask = 0;
            for (sk = 0; sk < kk; sk = sk + 1) begin
                q_addr[sk*AWID +: AWID] = sk << 20;
                q_len [sk*16   +: 16]   = n - 1;
                q_valid[sk] = 1'b1;
                rmask[sk]   = 1'b1;
                issued[sk]  = 8'd0;
            end
            words = n * bursts;
            while (got < words) begin
                @(negedge s_clk);
                for (sk = 0; sk < kk; sk = sk + 1) begin
                    if (q_valid[sk] && q_ready[sk]) begin
                        issued[sk] = issued[sk] + 8'd1;
                        if (issued[sk] >= bursts / kk) begin
                            q_valid[sk] = 1'b0;
                        end
                        else begin
                            q_addr[sk*AWID +: AWID] =
                                q_addr[sk*AWID +: AWID] + n * 32;
                        end
                    end
                end
            end
            measuring = 1'b0;
            q_valid = 0;
            case (kk)
                1:       report(n, bursts, lat, "k=1");
                2:       report(n, bursts, lat, "k=2");
                default: report(n, bursts, lat, "k=4");
            endcase
        end
    endtask

    // REQ2 writes while REQ reads, which is what a DRAM-to-DRAM copy asks of
    // the port. `rd` selects whether the read side runs at all.
    task wsweep(input integer n, input integer bursts, input integer lat,
                input rd);
        begin
            LAT = lat;
            @(negedge s_clk);
            got = 0; gotw = 0; measuring = 1'b1; seen_first = 1'b0;
            ar_cyc = -1; first_word = 0; cyc1 = 0; cyc1w = 0;
            cyc0 = scyc;
            q_addr[REQ*AWID  +: AWID] = 34'h0000_0000;
            q_addr[REQ2*AWID +: AWID] = 34'h0010_0000;
            q_len [REQ*16    +: 16]   = n - 1;
            q_len [REQ2*16   +: 16]   = n - 1;
            q_write[REQ2] = 1'b1;
            w_valid[REQ2] = 1'b1;
            q_valid[REQ] = rd; q_valid[REQ2] = 1'b1;
            rmask = 0; rmask[REQ] = 1'b1;
            nb = 0; nb2 = 0;
            words = n * bursts;
            while ((gotw < words) || (rd && (got < words))) begin
                @(negedge s_clk);
                if (q_valid[REQ] && q_ready[REQ]) begin
                    nb = nb + 1;
                    if (nb >= bursts) begin
                        q_valid[REQ] = 1'b0;
                    end
                    else begin
                        q_addr[REQ*AWID +: AWID] =
                            q_addr[REQ*AWID +: AWID] + n * 32;
                    end
                end
                if (q_valid[REQ2] && q_ready[REQ2]) begin
                    nb2 = nb2 + 1;
                    if (nb2 >= bursts) begin
                        q_valid[REQ2] = 1'b0;
                    end
                    else begin
                        q_addr[REQ2*AWID +: AWID] =
                            q_addr[REQ2*AWID +: AWID] + n * 32;
                    end
                end
            end
            measuring = 1'b0;
            q_write[REQ2] = 1'b0; w_valid[REQ2] = 1'b0;
            span = cyc1w - cyc0;
            wpc  = words * 1.0 / span;
            mbs  = wpc * 32.0 * mesh_mhz;
            $display("  %0s %0dMHz n=%0d LAT=%0d | wrote %0d words in %0d cyc = %0d.%03d w/cyc = %0d MB/s | read %0d words in %0d cyc",
                     rd ? "RD+WR" : "WR   ", $rtoi(mesh_mhz), n, lat, words, span,
                     $rtoi(wpc), $rtoi((wpc - $rtoi(wpc)) * 1000.0),
                     $rtoi(mbs), got, rd ? (cyc1 - cyc0) : 0);
        end
    endtask

    initial begin
        q_valid = 0; q_write = 0; q_addr = 0; q_len = 0;
        w_valid = 0; w_data = 0; r_ready = {N{1'b1}};
        measuring = 0; got = 0; ar_cyc = -1; rmask = 0; errors = 0;
        mbs_1 = 0; mbs_burst = 0;
        repeat (20) @(negedge s_clk);
        rstn = 1;
        repeat (20) @(negedge s_clk);

        // 100.09 MHz is where the card's numbers were taken, 150 the recorded
        // hardware ceiling, 300 what the BD asks clk_wiz_mesh for at power-on.
        for (i = 0; i < 3; i = i + 1) begin
            case (i)
                0: setclk(100.09);
                1: setclk(150.0);
                2: setclk(300.0);
            endcase
            sweepk(1,   64, LAT, 1);
            mbs_1 = $rtoi(mbs);
            sweepk(8,   32, LAT, 1);
            sweepk(16,  16, LAT, 1);
            sweepk(20,  32, LAT, 1);
            sweepk(32,  16, LAT, 1);
            sweepk(64,   8, LAT, 1);
            sweepk(128,  8, LAT, 1);
            sweepk(256,  8, LAT, 1);
            sweepk(20,  32, LAT, 2);
            sweepk(32,  16, LAT, 2);
            sweepk(64,  16, LAT, 2);
            mbs_burst = $rtoi(mbs);
            sweepk(128,  8, LAT, 2);
            sweepk(8,   32, LAT, 4);
            sweepk(20,  32, LAT, 4);
            sweepk(32,  16, LAT, 4);
            sweepk(64,  16, LAT, 4);
            wsweep(1,  256, LAT, 1'b0);
            wsweep(4,   64, LAT, 1'b0);
            wsweep(20,  32, LAT, 1'b0);
            wsweep(128,  8, LAT, 1'b0);
            wsweep(128,  8, LAT, 1'b1);
            sweepk(128,  8, 0, 1);
            // 5x, not the 10x this asserted before RD_OUT. The RTL did not get
            // worse: the one-word BASELINE got 4x faster, 188 -> 748 MB/s at

            // 300 MHz, because a single requester now holds four reads. The
            // ratio shrank while both ends improved, so the threshold moved.
            if (mbs_burst < 5 * mbs_1) begin
                errors = errors + 1;
                $display("  ERROR burst gain only %0d / %0d MB/s at %0d MHz",
                         mbs_burst, mbs_1, $rtoi(mesh_mhz));
            end
        end
        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- burst gain over 5x at every clock");
        end
        else begin
            $display("  FAIL -- %0d errors", errors);
        end
        $display("========================================");
        $finish;
    end
endmodule

`default_nettype wire
