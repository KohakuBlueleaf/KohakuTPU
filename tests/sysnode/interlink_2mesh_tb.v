// interlink_2mesh_tb -- the three transfer kinds, mesh 0 to mesh 1.
//
//   mesh0: mag_ilink + mag_switch  ── link0 ──  mag_switch + mag_ilink :mesh1
//
// The adapter and the switch, not whole MAGs. The pieces a whole MAG would add
// -- routers, clusters, a vector core -- are proved by run_noc_sim and
// run_system_sim and would only slow the thing that is actually new here: the
// address split, the encapsulator, the injector, and the doorbell's ordering
// against the writes it stands for.
//
// WHAT THE DOORBELL HAS TO MEAN. It is raised after the data has LANDED, so the
// far side holds it until every write ahead of it has its BRESP. This bench
// stalls mesh1's memory deliberately and checks the doorbell waits, because a
// doorbell that merely overtook a queue looks identical to a correct one right
// up until a consumer reads stale bytes.

`timescale 1ns / 1ps
`default_nettype none

module interlink_2mesh_tb;
    localparam integer LW = 288;
    localparam integer UW = 96;
    localparam integer FW = 288;
    localparam integer PW = 4;
    localparam integer DW = 256;
    localparam integer AW = 34;

    localparam [3:0] T_CU_DATA = 4'h8;

    reg clk = 0, resetn = 0;
    always begin
        #1.666 clk = ~clk;
    end

    integer errors = 0, checks = 0;

    task chk(input cond, input [1023:0] what);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                $display("  FAIL %0s", what);
            end
        end
    endtask

    // ---------------- mesh 0: the sender -----------------------------------
    reg  [AW-1:0] s0_awaddr;
    reg           s0_awvalid, s0_wvalid;
    reg  [DW-1:0] s0_wdata;
    wire          s0_awready, s0_wready, s0_bvalid;
    reg           s0_bready;

    reg  [FW-1:0] enc0_data;
    reg           enc0_valid;
    wire          enc0_busy;

    reg           cfg0_en;
    reg  [7:0]    cfg0_addr;
    reg  [63:0]   cfg0_data;
    reg  [3:0]    stat0_sel;
    wire [63:0]   stat0_q;
    wire [1:0]    mesh0;

    // ---------------- mesh 1: the receiver ---------------------------------
    reg  [3:0]  stat1_sel;
    wire [63:0] stat1_q;
    wire [1:0]  mesh1;
    wire [FW-1:0] inj1_data;
    wire          inj1_valid;
    reg           inj1_busy;

    // Its DRAM, and the stall that proves the doorbell waits for it.
    reg  [DW-1:0] mem1 [0:1023];
    reg           mem1_stall;

    wire [AW-1:0] lk1_awaddr;
    wire          lk1_awvalid, lk1_wvalid;
    wire [DW-1:0] lk1_wdata;
    reg           lk1_awready, lk1_wready, lk1_bvalid;

    // A write is captured when both channels have been accepted, and its BRESP
    // is deferred by `mem1_stall` -- which is the only lever this bench has on
    // the ordering it is trying to test.
    integer wr_seen = 0;
    reg [3:0] bq;                        // BRESPs owed
    always @(posedge clk) begin
        if (!resetn) begin
            lk1_awready <= 1'b0; lk1_wready <= 1'b0; lk1_bvalid <= 1'b0;
            bq <= 4'd0; wr_seen <= 0;
        end else begin
            lk1_awready <= !mem1_stall;
            lk1_wready  <= !mem1_stall;
            lk1_bvalid  <= 1'b0;
            if (lk1_awvalid && lk1_awready && lk1_wvalid && lk1_wready) begin
                mem1[lk1_awaddr[14:5]] <= lk1_wdata;
                wr_seen <= wr_seen + 1;
                bq <= bq + 4'd1;
            end
            if ((bq != 4'd0) && !mem1_stall) begin
                lk1_bvalid <= 1'b1;
                bq <= (
                    bq
                    - 4'd1
                    + (
                        (lk1_awvalid && lk1_awready && lk1_wvalid && lk1_wready)
                        ? 4'd1
                        : 4'd0
                    )
                );
            end
        end
    end

    // ---------------- the two ends and the link ----------------------------
    wire [AW-1:0] loc_awaddr;
    wire [DW-1:0] loc_wdata;
    wire          loc_awvalid, loc_wvalid;
    reg           loc_bvalid;
    always @(posedge clk) begin
        loc_bvalid <= loc_awvalid;
    end

    wire [LW-1:0] m0_td, m1_td, p0_td, p1_td;
    wire [UW-1:0] m0_tu, m1_tu, p0_tu, p1_tu;
    wire          m0_tl, m1_tl, p0_tl, p1_tl;
    wire          m0_tv, m1_tv, p0_tv, p1_tv;

    wire [UW-1:0] l0tx_h, l0rx_h, l1tx_h, l1rx_h;
    wire [LW-1:0] l0tx_d, l0rx_d, l1tx_d, l1rx_d;
    wire l0tx_hv, l0tx_hr, l0tx_dv, l0tx_dr, l0tx_dl;
    wire l0rx_hv, l0rx_hr, l0rx_dv, l0rx_dr, l0rx_dl;
    wire l1tx_hv, l1tx_hr, l1tx_dv, l1tx_dr, l1tx_dl;
    wire l1rx_hv, l1rx_hr, l1rx_dv, l1rx_dr, l1rx_dl;

    wire [63:0] s0_tx0, s0_rx0, s0_st0, s0_tx1, s0_rx1, s0_st1, s0_fwd, s0_lbk;
    wire [31:0] s0_c0, s0_c1;
    wire [3:0]  s0_flt;
    wire [63:0] s1_tx0, s1_rx0, s1_st0, s1_tx1, s1_rx1, s1_st1, s1_fwd, s1_lbk;
    wire [31:0] s1_c0, s1_c1;
    wire [3:0]  s1_flt;

    reg bad0;

    mag_ilink #(.MESH_ID(0)) IL0 (
        .clk(clk), .resetn(resetn),
        .cfg_en(cfg0_en), .cfg_addr(cfg0_addr), .cfg_data(cfg0_data),
        .stat_sel(stat0_sel), .stat_q(stat0_q), .my_mesh(mesh0),
        .s_awaddr(s0_awaddr), .s_awvalid(s0_awvalid), .s_awready(s0_awready),
        .s_wdata(s0_wdata), .s_wstrb({(DW/8){1'b1}}), .s_wvalid(s0_wvalid),
        .s_wready(s0_wready),
        .s_bvalid(s0_bvalid), .s_bresp(), .s_bready(s0_bready),
        .m_awaddr(loc_awaddr), .m_awvalid(loc_awvalid), .m_awready(1'b1),
        .m_wdata(loc_wdata), .m_wstrb(), .m_wlast(), .m_wvalid(loc_wvalid),
        .m_wready(1'b1), .m_bvalid(loc_bvalid), .m_bresp(2'b00), .m_bready(),
        .lk_awaddr(), .lk_awvalid(), .lk_awready(1'b1),
        .lk_wdata(), .lk_wstrb(), .lk_wlast(), .lk_wvalid(), .lk_wready(1'b1),
        .lk_bvalid(1'b0), .lk_bresp(2'b00), .lk_bready(),
        .enc_data(enc0_data), .enc_valid(enc0_valid), .enc_busy(enc0_busy),
        .inj_data(), .inj_valid(), .inj_busy(1'b0),
        .ltx_hdr(l0tx_h), .ltx_hvalid(l0tx_hv), .ltx_hready(l0tx_hr),
        .ltx_dat(l0tx_d), .ltx_dlast(l0tx_dl), .ltx_dvalid(l0tx_dv),
        .ltx_dready(l0tx_dr),
        .lrx_hdr(l0rx_h), .lrx_hvalid(l0rx_hv), .lrx_hready(l0rx_hr),
        .lrx_dat(l0rx_d), .lrx_dlast(l0rx_dl), .lrx_dvalid(l0rx_dv),
        .lrx_dready(l0rx_dr),
        .sw_tx0(s0_tx0), .sw_rx0(s0_rx0), .sw_stall0(s0_st0),
        .sw_tx1(s0_tx1), .sw_rx1(s0_rx1), .sw_stall1(s0_st1),
        .sw_fwd(s0_fwd), .sw_lblock(s0_lbk),
        .sw_cred0(s0_c0), .sw_cred1(s0_c1), .sw_fault(s0_flt),
        .bad_remote_req(bad0)
    );

    mag_switch #(.RX_BEATS(64), .MAX_BEATS(32)) SW0 (
        .clk(clk), .resetn(resetn), .my_mesh(mesh0),
        .ltx_hdr(l0tx_h), .ltx_hvalid(l0tx_hv), .ltx_hready(l0tx_hr),
        .ltx_dat(l0tx_d), .ltx_dlast(l0tx_dl), .ltx_dvalid(l0tx_dv),
        .ltx_dready(l0tx_dr),
        .lrx_hdr(l0rx_h), .lrx_hvalid(l0rx_hv), .lrx_hready(l0rx_hr),
        .lrx_dat(l0rx_d), .lrx_dlast(l0rx_dl), .lrx_dvalid(l0rx_dv),
        .lrx_dready(l0rx_dr),
        // mesh0 sits at the bottom of the chain, so its only neighbour is up
        // link1; link0 does not exist there and is tied off.
        .m0_tdata(), .m0_tuser(), .m0_tlast(), .m0_tvalid(), .m0_tready(1'b1),
        .s0_tdata({LW{1'b0}}), .s0_tuser({UW{1'b0}}), .s0_tlast(1'b0),
        .s0_tvalid(1'b0), .s0_tready(),
        .m1_tdata(m0_td), .m1_tuser(m0_tu), .m1_tlast(m0_tl), .m1_tvalid(m0_tv),
        .m1_tready(1'b1),
        .s1_tdata(p1_td), .s1_tuser(p1_tu), .s1_tlast(p1_tl), .s1_tvalid(p1_tv),
        .s1_tready(),
        .ctr_tx0(s0_tx0), .ctr_rx0(s0_rx0), .ctr_stall0(s0_st0),
        .ctr_tx1(s0_tx1), .ctr_rx1(s0_rx1), .ctr_stall1(s0_st1),
        .ctr_fwd(s0_fwd), .ctr_lblock(s0_lbk),
        .cred0_state(s0_c0), .cred1_state(s0_c1), .fault(s0_flt)
    );

    mag_ilink #(.MESH_ID(1)) IL1 (
        .clk(clk), .resetn(resetn),
        .cfg_en(1'b0), .cfg_addr(8'd0), .cfg_data(64'd0),
        .stat_sel(stat1_sel), .stat_q(stat1_q), .my_mesh(mesh1),
        .s_awaddr({AW{1'b0}}), .s_awvalid(1'b0), .s_awready(),
        .s_wdata({DW{1'b0}}), .s_wstrb({(DW/8){1'b1}}), .s_wvalid(1'b0),
        .s_wready(), .s_bvalid(), .s_bresp(), .s_bready(1'b1),
        .m_awaddr(), .m_awvalid(), .m_awready(1'b1),
        .m_wdata(), .m_wstrb(), .m_wlast(), .m_wvalid(), .m_wready(1'b1),
        .m_bvalid(1'b0), .m_bresp(2'b00), .m_bready(),
        .lk_awaddr(lk1_awaddr), .lk_awvalid(lk1_awvalid), .lk_awready(lk1_awready),
        .lk_wdata(lk1_wdata), .lk_wstrb(), .lk_wlast(), .lk_wvalid(lk1_wvalid),
        .lk_wready(lk1_wready),
        .lk_bvalid(lk1_bvalid), .lk_bresp(2'b00), .lk_bready(),
        .enc_data({FW{1'b0}}), .enc_valid(1'b0), .enc_busy(),
        .inj_data(inj1_data), .inj_valid(inj1_valid), .inj_busy(inj1_busy),
        .ltx_hdr(l1tx_h), .ltx_hvalid(l1tx_hv), .ltx_hready(l1tx_hr),
        .ltx_dat(l1tx_d), .ltx_dlast(l1tx_dl), .ltx_dvalid(l1tx_dv),
        .ltx_dready(l1tx_dr),
        .lrx_hdr(l1rx_h), .lrx_hvalid(l1rx_hv), .lrx_hready(l1rx_hr),
        .lrx_dat(l1rx_d), .lrx_dlast(l1rx_dl), .lrx_dvalid(l1rx_dv),
        .lrx_dready(l1rx_dr),
        .sw_tx0(s1_tx0), .sw_rx0(s1_rx0), .sw_stall0(s1_st0),
        .sw_tx1(s1_tx1), .sw_rx1(s1_rx1), .sw_stall1(s1_st1),
        .sw_fwd(s1_fwd), .sw_lblock(s1_lbk),
        .sw_cred0(s1_c0), .sw_cred1(s1_c1), .sw_fault(s1_flt),
        .bad_remote_req(1'b0)
    );

    mag_switch #(.RX_BEATS(64), .MAX_BEATS(32)) SW1 (
        .clk(clk), .resetn(resetn), .my_mesh(mesh1),
        .ltx_hdr(l1tx_h), .ltx_hvalid(l1tx_hv), .ltx_hready(l1tx_hr),
        .ltx_dat(l1tx_d), .ltx_dlast(l1tx_dl), .ltx_dvalid(l1tx_dv),
        .ltx_dready(l1tx_dr),
        .lrx_hdr(l1rx_h), .lrx_hvalid(l1rx_hv), .lrx_hready(l1rx_hr),
        .lrx_dat(l1rx_d), .lrx_dlast(l1rx_dl), .lrx_dvalid(l1rx_dv),
        .lrx_dready(l1rx_dr),
        .m0_tdata(m1_td), .m0_tuser(m1_tu), .m0_tlast(m1_tl), .m0_tvalid(m1_tv),
        .m0_tready(1'b1),
        .s0_tdata(p0_td), .s0_tuser(p0_tu), .s0_tlast(p0_tl), .s0_tvalid(p0_tv),
        .s0_tready(),
        .m1_tdata(), .m1_tuser(), .m1_tlast(), .m1_tvalid(), .m1_tready(1'b1),
        .s1_tdata({LW{1'b0}}), .s1_tuser({UW{1'b0}}), .s1_tlast(1'b0),
        .s1_tvalid(1'b0), .s1_tready(),
        .ctr_tx0(s1_tx0), .ctr_rx0(s1_rx0), .ctr_stall0(s1_st0),
        .ctr_tx1(s1_tx1), .ctr_rx1(s1_rx1), .ctr_stall1(s1_st1),
        .ctr_fwd(s1_fwd), .ctr_lblock(s1_lbk),
        .cred0_state(s1_c0), .cred1_state(s1_c1), .fault(s1_flt)
    );

    // Four register stages each way, which is a plausible SLR crossing and more
    // than the RTL guarantees -- the point being that credit makes it free.
    mag_link_pipe #(.DEPTH(4)) P01 (
        .clk(clk), .resetn(resetn), .tap(5'd4),
        .i_tdata(m0_td), .i_tuser(m0_tu), .i_tlast(m0_tl), .i_tvalid(m0_tv),
        .o_tdata(p0_td), .o_tuser(p0_tu), .o_tlast(p0_tl), .o_tvalid(p0_tv)
    );
    mag_link_pipe #(.DEPTH(4)) P10 (
        .clk(clk), .resetn(resetn), .tap(5'd4),
        .i_tdata(m1_td), .i_tuser(m1_tu), .i_tlast(m1_tl), .i_tvalid(m1_tv),
        .o_tdata(p1_td), .o_tuser(p1_tu), .o_tlast(p1_tl), .o_tvalid(p1_tv)
    );

    // ---------------- injected flits, captured ------------------------------
    reg [FW-1:0] got_flit [0:63];
    integer      n_inj = 0;
    always @(posedge clk) begin
        if (!resetn) begin
            n_inj <= 0;
        end
        else if (inj1_valid && !inj1_busy) begin
            got_flit[n_inj[5:0]] <= inj1_data;
            n_inj <= n_inj + 1;
        end
    end

    // ---------------- stimulus ---------------------------------------------
    function [DW-1:0] patt(input [31:0] s);
        integer k;
        begin
            for (k = 0; k < DW/32; k = k + 1) begin
                patt[k*32 +: 32] = s + k[31:0];
            end
        end
    endfunction

    task automatic wr(input [AW-1:0] a, input [DW-1:0] d);
        begin
            s0_awaddr <= a; s0_awvalid <= 1'b1;
            @(posedge clk);
            while (!s0_awready) begin
                @(posedge clk);
            end
            s0_awvalid <= 1'b0;
            s0_wdata <= d; s0_wvalid <= 1'b1;
            @(posedge clk);
            while (!s0_wready) begin
                @(posedge clk);
            end
            s0_wvalid <= 1'b0;
            s0_bready <= 1'b1;
            @(posedge clk);
            while (!s0_bvalid) begin
                @(posedge clk);
            end
            s0_bready <= 1'b0;
        end
    endtask

    task automatic push_flit(input [FW-1:0] f);
        begin
            enc0_data <= f; enc0_valid <= 1'b1;
            @(posedge clk);
            while (enc0_busy) begin
                @(posedge clk);
            end
            enc0_valid <= 1'b0;
        end
    endtask

    function [FW-1:0] cud(input [3:0] dx, input [3:0] dy,
                          input [3:0] sx, input [3:0] sy,
                          input [7:0] fin, input [1:0] mesh,
                          input last, input [255:0] pay);
        begin
            cud = {dx, dy, sx, sy, T_CU_DATA, fin, last, 1'b1, mesh, pay};
        end
    endfunction

    integer i, tx_was, t0;
    reg [63:0] w;

    initial begin
        s0_awvalid = 0; s0_wvalid = 0; s0_bready = 0; s0_awaddr = 0; s0_wdata = 0;
        enc0_valid = 0; enc0_data = 0;
        cfg0_en = 0; cfg0_addr = 0; cfg0_data = 0;
        stat0_sel = 0; stat1_sel = 0;
        inj1_busy = 0; mem1_stall = 0; bad0 = 0;
        for (i = 0; i < 1024; i = i + 1) begin
            mem1[i] = {DW{1'b0}};
        end
        repeat (8) @(posedge clk);
        resetn <= 1'b1;
        repeat (8) @(posedge clk);

        $display("=== interlink, two meshes ===");

        // ---- 1. capability discovery, both ends ------------------------
        stat0_sel <= 4'd0; @(posedge clk); @(posedge clk);
        chk(stat0_q[15:0] == 16'h494C, "IL_CAPS magic on mesh 0");
        chk(stat0_q[23:20] == 4'd0,    "IL_CAPS reports mesh 0");
        stat1_sel <= 4'd0; @(posedge clk); @(posedge clk);
        chk(stat1_q[23:20] == 4'd1,    "IL_CAPS reports mesh 1");

        // ---- 2. a LOCAL write must not touch the link -------------------
        // link1 is mesh0's only link on the chain, so it is the one that would
        // carry a leak; its link0 counter is dead and would pass regardless.
        tx_was = s0_tx1[31:0];
        wr(34'h0_0000_0100, patt(32'hAAAA));
        repeat (20) @(posedge clk);
        chk(s0_tx1[31:0] == tx_was, "a local write left through the link");
        chk(wr_seen == 0, "a local write reached the far mesh");

        // ---- 3. remote writes land byte-exact ---------------------------
        for (i = 0; i < 8; i = i + 1) begin
            wr(34'h1_0000_0000 + i * 32, patt(32'h1000 + i));
        end
        repeat (200) @(posedge clk);
        chk(wr_seen == 8, "all eight remote writes arrived");
        for (i = 0; i < 8; i = i + 1) begin
            chk(mem1[i] === patt(32'h1000 + i), "remote write payload");
        end

        // ---- 4. the doorbell waits for the data ------------------------
        // mesh1's memory is stalled, so the writes are accepted into the link
        // but cannot retire. The doorbell is sent behind them and must NOT
        // count until their BRESPs are back.
        mem1_stall <= 1'b1;
        repeat (4) @(posedge clk);
        for (i = 0; i < 4; i = i + 1) begin
            wr(34'h1_0000_1000 + i * 32, patt(32'h2000 + i));
        end
        cfg0_en <= 1'b1; cfg0_addr <= 8'h90; cfg0_data <= {48'd0, 8'h5A, 6'd0, 2'd1};
        @(posedge clk);
        cfg0_en <= 1'b0;
        repeat (200) @(posedge clk);

        stat1_sel <= 4'd2; @(posedge clk); @(posedge clk);
        chk(stat1_q[31:0] == 32'd0,
            "the doorbell counted while its data was still in the write queue");

        mem1_stall <= 1'b0;
        repeat (300) @(posedge clk);
        stat1_sel <= 4'd2; @(posedge clk); @(posedge clk);
        chk(stat1_q[31:0] == 32'd1, "the doorbell arrived once the data had landed");
        chk(stat1_q[47:32] == 16'h5A, "the doorbell carried its txn");
        for (i = 0; i < 4; i = i + 1) begin
            chk(mem1[16'h80 + i] === patt(32'h2000 + i),
                "stalled remote write payload");
        end

        // ---- 5. a CU_DATA burst crosses and is re-addressed --------------
        // Descriptor plus three data flits, from (5,6) to node (3,3) of mesh 1.
        push_flit(cud(4'd0, 4'd1, 4'd5, 4'd6, 8'h33, 2'd1, 1'b0,
                      {8'd0, 16'd0, 8'd2, 8'h01, 8'h22, 208'd0}));
        for (i = 0; i < 3; i = i + 1) begin
            push_flit(cud(4'd0, 4'd1, 4'd5, 4'd6, 8'h33, 2'd1, (i == 2),
                          {224'd0, 32'hC0DE_0000 + i}));
        end
        repeat (300) @(posedge clk);

        chk(n_inj == 4, "every flit of the burst was injected");
        for (i = 0; i < 4 && i < n_inj; i = i + 1) begin
            chk(got_flit[i][287 -: 4] == 4'd3, "injected dst_x is the final node");
            chk(got_flit[i][283 -: 4] == 4'd3, "injected dst_y is the final node");
            chk(got_flit[i][279 -: 4] == 4'd5, "the source coordinate survived");
            chk(got_flit[i][275 -: 4] == 4'd6, "the source coordinate survived");
            chk(got_flit[i][267 -: 8] == 8'd0, "txn was restored to zero");
            chk(got_flit[i][258 -: 3] == 3'd0, "the remote marker was cleared");
        end
        if (n_inj == 4) begin
            chk(got_flit[0][215 -: 8] == 8'h22, "the ack destination survived");
            chk(got_flit[3][31:0] == 32'hC0DE_0002, "the last payload word");
            chk(got_flit[3][259] == 1'b1, "NOC_LAST survived the crossing");
        end

        // ---- 6. a remote memory request faults rather than hanging -------
        bad0 <= 1'b1; @(posedge clk); bad0 <= 1'b0;
        repeat (10) @(posedge clk);
        stat0_sel <= 4'd1; @(posedge clk); @(posedge clk);
        chk(stat0_q[0] == 1'b1, "IL_F_RD_REMOTE was raised");

        // ---- 7. no routing faults anywhere ------------------------------
        chk(s0_flt == 4'd0, "mesh 0's switch raised a fault");
        chk(s1_flt == 4'd0, "mesh 1's switch raised a fault");

        // ---- 8. the numbers, for docs/interlink ------------------------
        stat0_sel <= 4'd8; @(posedge clk); @(posedge clk);
        $display("  link1 out of mesh 0: %0d packets, %0d beats",
                 stat0_q[31:0], stat0_q[63:32]);
        stat0_sel <= 4'd11; @(posedge clk); @(posedge clk);
        $display("  link1 credit-stalled %0d cycles, idle %0d",
                 stat0_q[31:0], stat0_q[63:32]);

        $display("--- %0d checks, %0d errors", checks, errors);
        if (errors == 0) begin
            $display("PASS interlink_2mesh");
        end
        else begin
            $display("FAIL interlink_2mesh");
        end
        $finish;
    end

    initial begin
        #2_000_000;
        $display("WATCHDOG interlink_2mesh_tb -- no verdict.");
        $display("FAIL interlink_2mesh");
        $finish;
    end
endmodule

`default_nettype wire
