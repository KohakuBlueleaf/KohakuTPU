// FILL AND DRAIN THROUGHPUT, one clock: a 128-entry FILL from DRAM and one from
// MAG staging, each timed from the instruction to the fill retiring, and a
// 512-sub-tile DRAIN to DRAM timed to its last W beat.
//
// At the memory port's output it counts flits sent and cycles held by
// backpressure; at the cluster's port, flits taken. A port running at line
// rate sends a flit every cycle, 4 per 128-byte entry. -d STG_IN_MAG puts the
// staging store in MAG rather than at the port.

`timescale 1ns/1ps

module mm_fill_rate_tb;
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4;
    localparam MEMP = 1, NCH = 1;
    localparam [3:0] T_CU_INST = 4'h5;
    localparam CX = 1, CY = 1;
    localparam LX = 2, LY = 1;
    localparam [39:0] A_STG = 40'h80_0000_0000;
    localparam integer N = 128;

    reg clk = 0, rst = 1;
    always begin
        #2 clk = ~clk;
    end

    reg  [FW-1:0] ext_i;
    reg           ext_iv;
    wire          ext_ib;
    wire [FW-1:0] ext_o;
    wire          ext_ov;

    wire [NCH*IDW-1:0]  r_awid, r_arid, r_bid, r_rid;
    wire [NCH*AW-1:0]   r_awaddr, r_araddr;
    wire [NCH*8-1:0]    r_awlen, r_arlen;
    wire [NCH*3-1:0]    r_awsize, r_arsize;
    wire [NCH*2-1:0]    r_awburst, r_arburst, r_bresp, r_rresp;
    wire [NCH-1:0]      r_awvalid, r_awready, r_wvalid, r_wready, r_wlast;
    wire [NCH-1:0]      r_bvalid, r_bready, r_arvalid, r_arready;
    wire [NCH-1:0]      r_rvalid, r_rready, r_rlast;
    wire [NCH*DW-1:0]   r_wdata, r_rdata;
    wire [NCH*DW/8-1:0] r_wstrb;

    wire [47:0] dbg_cluster;
    wire [31:0] dbg_vcyc, obs;
    wire        dbg_vflt;

    mm_mesh #(.FW(FW), .PW(PW), .DW(DW), .AW(AW), .IDW(IDW), .MEMP(MEMP),
              .MODEL(1), .L2_CU(0),
              .L2_MAG(1), .L2_MAG_BANKS(4), .L2_MAG_ENTRIES(1024),
              .L2_MAG_MESH(0),
`ifdef STG_IN_MAG
              .L2_MAG_AT_PORT(0)
`else
              .L2_MAG_AT_PORT(1)
`endif
              ) dut (
        .clk(clk), .mat_clk(clk), .vec_clk(clk), .rst(rst),
        .sm_awaddr({AW{1'b0}}), .sm_awlen(8'd0), .sm_awvalid(1'b0),
        .sm_wdata({DW{1'b0}}), .sm_wlast(1'b0), .sm_wvalid(1'b0),
        .sc_awaddr(32'd0), .sc_awvalid(1'b0), .sc_awready(),
        .sc_wdata(64'd0), .sc_wvalid(1'b0), .sc_wready(),
        .sc_bvalid(),
        .sc_araddr(32'd0), .sc_arvalid(1'b0),
        .sc_rdata(), .sc_rvalid(),
        .mv_busy(), .mv_fault(), .mv_done(),
        .dram_aclk(clk), .dram_aresetn(!rst),
        .dram_awid(r_awid), .dram_awaddr(r_awaddr), .dram_awlen(r_awlen),
        .dram_awsize(r_awsize), .dram_awburst(r_awburst),
        .dram_awvalid(r_awvalid), .dram_awready(r_awready),
        .dram_wdata(r_wdata), .dram_wstrb(r_wstrb), .dram_wlast(r_wlast),
        .dram_wvalid(r_wvalid), .dram_wready(r_wready),
        .dram_bid(r_bid), .dram_bresp(r_bresp), .dram_bvalid(r_bvalid),
        .dram_bready(r_bready),
        .dram_arid(r_arid), .dram_araddr(r_araddr), .dram_arlen(r_arlen),
        .dram_arsize(r_arsize), .dram_arburst(r_arburst),
        .dram_arvalid(r_arvalid), .dram_arready(r_arready),
        .dram_rid(r_rid), .dram_rdata(r_rdata), .dram_rresp(r_rresp),
        .dram_rlast(r_rlast), .dram_rvalid(r_rvalid), .dram_rready(r_rready),
        .ext_in_data(ext_i), .ext_in_valid(ext_iv), .ext_in_busy(ext_ib),
        .ext_out_data(ext_o), .ext_out_valid(ext_ov), .ext_out_busy(1'b0),
        .dbg_cluster(dbg_cluster), .dbg_vec_cycles(dbg_vcyc),
        .dbg_vec_fault(dbg_vflt), .obs(obs)
    );

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .WORDS(4096),
              .PORTS(NCH)) u_ram (
        .clk(clk), .resetn(!rst),
        .s_awid(r_awid), .s_awaddr(r_awaddr), .s_awlen(r_awlen),
        .s_awsize(r_awsize), .s_awburst(r_awburst),
        .s_awvalid(r_awvalid), .s_awready(r_awready),
        .s_wdata(r_wdata), .s_wstrb(r_wstrb), .s_wlast(r_wlast),
        .s_wvalid(r_wvalid), .s_wready(r_wready),
        .s_bid(r_bid), .s_bresp(r_bresp), .s_bvalid(r_bvalid),
        .s_bready(r_bready),
        .s_arid(r_arid), .s_araddr(r_araddr), .s_arlen(r_arlen),
        .s_arsize(r_arsize), .s_arburst(r_arburst),
        .s_arvalid(r_arvalid), .s_arready(r_arready),
        .s_rid(r_rid), .s_rdata(r_rdata), .s_rresp(r_rresp),
        .s_rlast(r_rlast), .s_rvalid(r_rvalid), .s_rready(r_rready),
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DW{1'b0}}), .bd_rdata()
    );

    // ---- probes ------------------------------------------------------------
    // Every counter has ONE writer, this process; the stimulus clears them
    // through `clr`: two writers per counter drop updates under Verilator.
    reg  [31:0] cyc = 0;
    reg  [31:0] mag_sent = 0, mag_held = 0, cu_took = 0, cu_held = 0;
    reg  [31:0] first_mag = 0, last_mag = 0;
    reg  [31:0] w_beats = 0, w_first = 0, w_last = 0;
    reg         clr = 1'b0;
    wire        mag_go = dut.mag_ov && !dut.mag_ob;
    wire        w_go   = r_wvalid[0] && r_wready[0];
    always @(posedge clk) if (!rst) begin
        cyc <= cyc + 1;
        if (clr) begin
            mag_sent <= 0; mag_held <= 0; cu_took <= 0; cu_held <= 0;
            first_mag <= 0; last_mag <= 0; w_beats <= 0; w_first <= 0; w_last <= 0;
        end else begin
            if (mag_go) begin
                if (mag_sent == 0) first_mag <= cyc;
                mag_sent <= mag_sent + 1;
                last_mag <= cyc;
            end
            if (w_go) begin
                if (w_beats == 0) w_first <= cyc;
                w_beats <= w_beats + 1;
                w_last <= cyc;
            end
            if (dut.mag_ov && dut.mag_ob) mag_held <= mag_held + 1;
            if (dut.u_cluster.noc_in_valid && !dut.u_cluster.noc_in_busy) cu_took <= cu_took + 1;
            if (dut.u_cluster.noc_in_valid && dut.u_cluster.noc_in_busy) cu_held <= cu_held + 1;
        end
    end

    task clear_probes;
        begin
            @(negedge clk);
            clr = 1'b1;
            @(negedge clk);
            clr = 1'b0;
        end
    endtask

`ifdef TRACE_AXI
    // Every AR accepted and R beat delivered at the RAM, for the first entries.
    integer axi_left = 0;
    always @(posedge clk) if (!rst && axi_left > 0 && (r_arvalid[0] || r_rvalid[0])) begin
        axi_left = axi_left - 1;
        $display("  %0d AR v%0d r%0d addr=%h len=%0d | R v%0d r%0d last=%0d", cyc,
                 r_arvalid[0], r_arready[0], r_araddr[AW-1:0], r_arlen[7:0],
                 r_rvalid[0], r_rready[0], r_rlast[0]);
    end
`endif

`ifdef TRACE_PORT
    // The read engine, cycle by cycle, for the first entries of a traced fill.
    integer trace_left = 0;
    always @(posedge clk) if (!rst && trace_left > 0) begin
        trace_left = trace_left - 1;
        $display("  %0d m_ar v%0d r%0d len=%0d out=%0d | m_r v%0d r%0d | r v%0d r%0d | rs=%0d q_rdy=%0d e_act=%0d q_emit=%0d p_cnt=%0d ent=%0d | dram ar v%0d r%0d r v%0d r%0d | mag_ov=%0d mag_ob=%0d",
                 cyc,
                 dut.u_mag.u_mag.g_port[0].u_eng.m_arvalid, dut.u_mag.u_mag.g_port[0].u_eng.m_arready,
                 dut.u_mag.u_mag.g_port[0].u_eng.m_arlen, dut.u_mag.u_mag.g_port[0].u_eng.ar_out,
                 dut.u_mag.u_mag.g_port[0].u_eng.m_rvalid, dut.u_mag.u_mag.g_port[0].u_eng.m_rready,
                 dut.u_mag.u_mag.g_port[0].u_eng.r_valid, dut.u_mag.u_mag.g_port[0].u_eng.r_ready,
                 dut.u_mag.u_mag.g_port[0].u_eng.rs, dut.u_mag.u_mag.g_port[0].u_eng.q_rdy,
                 dut.u_mag.u_mag.g_port[0].u_eng.e_act, dut.u_mag.u_mag.g_port[0].u_eng.q_emit,
                 dut.u_mag.u_mag.g_port[0].u_eng.p_cnt, dut.u_mag.u_mag.g_port[0].u_eng.rd_ent,
                 r_arvalid[0], r_arready[0], r_rvalid[0], r_rready[0], dut.mag_ov, dut.mag_ob);
    end
`endif

    task send_flit(input [3:0] dx, input [3:0] dy, input [3:0] ty,
                   input [7:0] txn, input lst, input [255:0] payload);
        begin
            @(negedge clk);
            while (ext_ib) begin
                @(negedge clk);
            end
            ext_i  <= {dx, dy, CX[3:0], CY[3:0], ty, txn, lst, 3'b000, payload};
            ext_iv <= 1'b1;
            @(negedge clk);
            ext_iv <= 1'b0;
        end
    endtask

    task cl_fill(input [39:0] addr, input [15:0] n);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 4]  = 4'd1;
            p[251 -: 34] = addr[33:0];
            p[68  -: 6]  = addr[39:34];
            p[217 -: 16] = n;
            send_flit(LX[3:0], LY[3:0], T_CU_INST, 8'h40, 1'b0, p);
        end
    endtask

    integer errors = 0, t0, fills0;

    task timed_fill(input [39:0] addr, input [255:0] what);
        begin
            clear_probes;
            fills0 = dbg_cluster[47:32];
            t0 = cyc;
            cl_fill(addr, N[15:0]);
            while ((dbg_cluster[47:32] == fills0[15:0]) && (cyc - t0 < 200000)) begin
                @(negedge clk);
            end
            if (cyc - t0 >= 200000) begin
                errors = errors + 1;
                $display("  FAIL %0s: the fill never retired", what);
            end
            // Line rate is 4 flits an entry; the bound leaves room for the round trip.
            if (cyc - t0 > 5 * N) begin
                errors = errors + 1;
                $display("  FAIL %0s: over 5 cycles an entry", what);
            end
            $display("  %0s: %0d entries in %0d cycles (%0d.%02d per entry); MAG sent %0d flits over %0d cycles, held %0d; cluster took %0d, refused %0d",
                     what, N, cyc - t0, (cyc - t0) / N, ((cyc - t0) % N) * 100 / N,
                     mag_sent, last_mag - first_mag + 1, mag_held, cu_took, cu_held);
        end
    endtask

    // A DRAIN of ND sub-tiles to DRAM, timed to its last W beat at the RAM:
    // one 256-bit beat per sub-tile, so line rate is one beat a cycle.
    localparam integer ND = 512;
    task timed_drain(input [39:0] addr);
        reg [255:0] p;
        integer d0;
        begin
            p = 256'd0;
            p[255 -: 4]  = 4'd3;
            p[251 -: 34] = addr[33:0];
            p[68  -: 6]  = addr[39:34];
            p[217 -: 16] = ND[15:0];
            clear_probes;
            d0 = dbg_cluster[15:0];
            t0 = cyc;
            send_flit(LX[3:0], LY[3:0], T_CU_INST, 8'h41, 1'b0, p);
            while ((w_beats < ND) && (cyc - t0 < 200000)) begin
                @(negedge clk);
            end
            if (w_beats < ND) begin
                errors = errors + 1;
                $display("  FAIL drain: %0d of %0d beats reached the RAM", w_beats, ND);
            end
            if (dbg_cluster[15:0] == d0[15:0]) begin
                errors = errors + 1;
                $display("  FAIL drain: the DRAIN never retired");
            end
            if (2 * (w_last - t0) > 3 * ND) begin
                errors = errors + 1;
                $display("  FAIL drain: over 1.5 cycles a sub-tile");
            end
            $display("  drain  : %0d sub-tiles in %0d cycles (%0d.%02d per sub-tile); W beats %0d over %0d cycles; retired at %0d",
                     ND, w_last - t0, (w_last - t0) / ND, ((w_last - t0) % ND) * 100 / ND,
                     w_beats, w_last - w_first + 1, cyc - t0);
        end
    endtask

    integer i;
    initial begin
        ext_i = 0; ext_iv = 0;
        for (i = 0; i < 4096; i = i + 1) begin
            u_ram.mem[i] = 256'd0;
        end
        repeat (10) @(negedge clk);
        rst = 0;
        repeat (10) @(negedge clk);

`ifdef TRACE_AXI
        axi_left = 40;
`endif
`ifdef TRACE_PORT
        trace_left = 120;
`endif
        timed_fill(40'd0, "DRAM   ");
        repeat (50) @(negedge clk);
        timed_fill(A_STG, "staging");
        repeat (50) @(negedge clk);
        timed_drain(40'h1_0000);

        if (errors == 0) $display("PASS -- fill and drain at their bounds");
        else $display("FAIL -- %0d errors", errors);
        $finish;
    end
endmodule
