// MAG STAGING PROVED, not merely observed: a cluster fills from special
// aperture 0 over a real mesh, and the check can tell staging from an alias.

// A_STG aliases onto the DRAM word holding the same golden tile, so a check
// that only compares the two passes whether or not bit 39 was ever honoured.

// So: DRAM is POISONED under the staged copy, and every AXI channel is watched
// for a special-aperture address. No noc_l2_adapter here -- L2_CU=0.

// SECTION ORDER IS LOAD-BEARING. Re-ordering does not fail; it makes the later
// sections pass vacuously.

// A dropped WRITE costs the mesh ~80 ms of stall. A dropped READ parks the
// cluster in S_FILL for good, so exactly ONE fits and it goes last.

// Hence aperture 3's READ is mm_mesh_l2_tb s6's, not this bench's. Broken once
// already: a fill believed to name mesh 1 named aperture 1, and s4-s5 never ran.

`default_nettype none
`timescale 1ns/1ps

module mm_mesh_stage_tb;
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4;
    localparam MEMP = 1, NCH = 1;

    localparam [3:0] T_CU_INST = 4'h5, T_CU_SIGNAL = 4'h6, T_CU_DATA = 4'h8;

    localparam CX = 1, CY = 1;
    localparam LX = 2, LY = 1;

    localparam integer SBIAS = 20;
    localparam [7:0] ANCHOR = 8'd40;

    // [39]=1 special, [38]=0, [37:36] mesh 0, [35:32] aperture. 0 is staging,
    // 3 is reserved and must fault rather than alias.
    localparam [39:0] A_STG    = 40'h80_0000_0000;
    localparam [39:0] A_RSVD_R = 40'h83_0000_0000;
    // Aliases onto DRAM word 100 if bit 39 is ignored: (addr >> 5) truncated to
    // the RAM's 12 index bits. Chosen to land on a region known to be zero.
    localparam [39:0] A_RSVD_W = 40'h83_0000_0C80;
    // Mesh 1 aperture 0. Mesh is [37:36], so +0x10_ and NOT +0x01_: 0x81_ is
    // THIS mesh's aperture 1, which stg_bad drops, wedging every later section.
    localparam [39:0] A_STG_M1 = 40'h90_0000_0000;
    localparam integer W_SPY = 100;

    localparam integer W_REF = 1024, W_POI = 1025, W_STG = 1026;

    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

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
              .MODEL(1),
              .L2_CU(0),
              // The shipped 4-bank shape, shortened so the simulator is not
              // asked to model 2 MB of URAM.
              .L2_MAG(1), .L2_MAG_BANKS(4), .L2_MAG_ENTRIES(1024),
              .L2_MAG_MESH(0),
              // -d MM_L2_PORT runs the SAME checks with the store moved onto
              // MAG's converged path, so the two placements are one A/B.
`ifdef MM_L2_PORT
              .L2_MAG_AT_PORT(1)
`else
              .L2_MAG_AT_PORT(0)
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

    reg           bd_we = 0;
    reg  [15:0]   bd_addr = 0;
    reg  [DW-1:0] bd_wdata = 0;
    wire [DW-1:0] bd_rdata;

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
        .bd_we(bd_we), .bd_addr(bd_addr), .bd_wdata(bd_wdata),
        .bd_rdata(bd_rdata)
    );

    integer errors = 0, checks = 0, spin;

    task chk(input cond, input [255:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 20) $display("  FAIL %0s [%0d]", what, where);
            end
        end
    endtask

    // A special-aperture address must never reach AXI at all. axi_ram truncates
    // the index to 12 bits, as a DDR4 controller does: it lands, it never errors.
    integer ax_aw_special = 0, ax_ar_special = 0, ax_ar_any = 0;
    integer ci;
    always @(posedge clk) if (!rst) begin
        if (r_arvalid[0] && r_arready[0]) ax_ar_any = ax_ar_any + 1;
        for (ci = 0; ci < NCH; ci = ci + 1) begin
            if (r_awvalid[ci] && r_awaddr[ci*AW + 39]) begin
                ax_aw_special = ax_aw_special + 1;
                if (ax_aw_special < 5)
                    $display("%0t ERROR bench: AW on channel %0d carries %h -- a special aperture reached AXI",
                             $time, ci, r_awaddr[ci*AW +: AW]);
            end
            if (r_arvalid[ci] && r_araddr[ci*AW + 39]) begin
                ax_ar_special = ax_ar_special + 1;
                if (ax_ar_special < 5)
                    $display("%0t ERROR bench: AR on channel %0d carries %h -- a special aperture reached AXI",
                             $time, ci, r_araddr[ci*AW +: AW]);
            end
        end
    end

    // A dropped request is indistinguishable from one never sent, and a wedged
    // cluster sends none -- so count what actually ARRIVES at MAG.
    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    wire [3:0]  magi_ty   = dut.mag_i[FW-4*PW-1 -: 4];
    wire [39:0] magi_addr = dut.mag_i[255 -: 40];
    wire        magi_rsvd = magi_addr[39] && !magi_addr[38] &&
                            (magi_addr[37:36] == 2'd0) && (magi_addr[35:32] != 4'h0);

    wire magi_rem = magi_addr[39] && !magi_addr[38] &&
                    (magi_addr[37:36] != 2'd0);

    integer mag_rsvd_rd = 0, mag_rsvd_wr = 0, mag_rem_rd = 0, mag_rem_wr = 0;
    always @(posedge clk) if (!rst && dut.mag_iv && !dut.mag_ib) begin
        if (magi_rsvd && magi_ty == T_MEM_RD_REQ) mag_rsvd_rd = mag_rsvd_rd + 1;
        if (magi_rsvd && magi_ty == T_MEM_WR_REQ) mag_rsvd_wr = mag_rsvd_wr + 1;
        if (magi_rem  && magi_ty == T_MEM_RD_REQ) mag_rem_rd  = mag_rem_rd + 1;
        if (magi_rem  && magi_ty == T_MEM_WR_REQ) mag_rem_wr  = mag_rem_wr + 1;
    end

    // ================================================ the bench as the agent
    task send_flit(input [3:0] dx, input [3:0] dy, input [3:0] ty,
                   input [7:0] txn, input lst, input [255:0] payload);
        begin
            @(negedge clk);
            while (ext_ib) @(negedge clk);
            ext_i  <= {dx, dy, CX[3:0], CY[3:0], ty, txn, lst, 3'b000, payload};
            ext_iv <= 1'b1;
            @(negedge clk);
            ext_iv <= 1'b0;
        end
    endtask

    task cl_inst(input [255:0] p);
        begin send_flit(LX[3:0], LY[3:0], T_CU_INST, 8'h40, 1'b0, p); end
    endtask

    integer sig_cl = 0;
    wire [3:0] o_type = ext_o[FW-4*PW-1 -: 4];
    always @(posedge clk) if (!rst && ext_ov)
        if (o_type == T_CU_SIGNAL) sig_cl = sig_cl + 1;

    // ================================================ operands, over CU_DATA
    localparam integer MAXG = 4, MAXNK = 2;
    localparam integer MAXM = MAXG*4, MAXK = MAXNK*32;

    integer signed A [0:MAXM-1][0:MAXK-1];
    integer signed B [0:MAXK-1][0:MAXM-1];
    integer        SA [0:MAXM-1][0:MAXNK-1];
    integer        SB [0:MAXM-1][0:MAXNK-1];
    integer        seed = 7;

    function [255:0] word_a(input integer row0, input integer kb, input integer w);
        integer ii, kk;
        begin
            word_a = 256'd0;
            for (ii = 0; ii < 4; ii = ii + 1)
                for (kk = 0; kk < 8; kk = kk + 1)
                    word_a[255 - (ii*8+kk)*7 -: 7] = A[row0+ii][kb*32 + w*8 + kk][6:0];
        end
    endfunction

    function [255:0] word_b(input integer col0, input integer kb, input integer w);
        integer jj, kk;
        begin
            word_b = 256'd0;
            for (kk = 0; kk < 8; kk = kk + 1)
                for (jj = 0; jj < 4; jj = jj + 1)
                    word_b[255 - (kk*4+jj)*7 -: 7] = B[kb*32 + w*8 + kk][col0+jj][6:0];
        end
    endfunction

    function [255:0] with_scales(input [255:0] w, input integer lane0,
                                 input integer kb, input integer side);
        integer ii;
        begin
            with_scales = w;
            for (ii = 0; ii < 4; ii = ii + 1)
                with_scales[31 - ii*8 -: 8] =
                    ((side == 0 ? SA[lane0+ii][kb] : SB[lane0+ii][kb]) + SBIAS) << 3;
        end
    endfunction

    task fill_side(input integer side, input integer ng, input integer nk);
        integer e, w, ne;
        reg [255:0] p;
        begin
            ne = ng*nk;
            p = 256'd0;
            p[255 -: 8]  = side[7:0];
            p[247 -: 16] = 16'd0;
            p[231 -: 8]  = (ne*4 - 1);
            send_flit(LX[3:0], LY[3:0], T_CU_DATA, 8'h00, 1'b0, p);
            for (e = 0; e < ne; e = e + 1)
                for (w = 0; w < 4; w = w + 1) begin
                    p = (side == 0) ? word_a((e/nk)*4, e % nk, w)
                                    : word_b((e/nk)*4, e % nk, w);
                    if (w == 0) p = with_scales(p, (e/nk)*4, e % nk, side);
                    send_flit(LX[3:0], LY[3:0], T_CU_DATA, 8'h00,
                              (e == ne-1) && (w == 3), p);
                end
        end
    endtask

    task send_gemm(input integer gm, input integer gn, input integer nk);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 4] = 4'd2;
            p[199 -: 8] = gm[7:0];
            p[191 -: 8] = gn[7:0];
            p[183 -: 8] = nk[7:0];
            p[175 -: 8] = ANCHOR;
            cl_inst(p);
        end
    endtask

    // The 40-bit address is SPLIT: [33:0] in place and [39:34] in the tail
    // field at [68:63], because the instruction flit is packed solid.
    task drain_at(input integer nt, input [39:0] addr);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 4]  = 4'd3;
            p[251 -: 34] = addr[33:0];
            p[68  -: 6]  = addr[39:34];
            p[217 -: 16] = nt[15:0];
            p[175 -: 8]  = ANCHOR;
            cl_inst(p);
        end
    endtask

    // preq is bit 141, NOT 200 -- 200 is GEMM's `acc`. At preq=0 MAG quantises,
    // which takes the DRAM branch and never consults staging at all.
    task cl_fill(input [39:0] addr, input [15:0] n, input sel, input preq);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 4]  = 4'd1;
            p[251 -: 34] = addr[33:0];
            p[68  -: 6]  = addr[39:34];
            p[217 -: 16] = n;
            p[201]       = sel;
            p[141]       = preq;
            cl_inst(p);
        end
    endtask

    task wait_drains(input integer n, input integer limit);
        begin
            spin = 0;
            while ((dbg_cluster[15:0] < n[15:0]) && (spin < limit)) begin
                spin = spin + 1; @(negedge clk);
            end
            chk(spin < limit, "the cluster retired its drains", n);
            repeat (400) @(negedge clk);
        end
    endtask

    task wait_fills(input integer n, input integer limit);
        begin
            spin = 0;
            while ((dbg_cluster[47:32] < n[15:0]) && (spin < limit)) begin
                spin = spin + 1; @(negedge clk);
            end
            chk(spin < limit, "the cluster retired its fills", n);
        end
    endtask

    task bd_read(input integer word);
        begin
            @(negedge clk); bd_addr = word[15:0];
            @(negedge clk); @(negedge clk);
        end
    endtask

    task bd_write(input integer word, input [255:0] v);
        begin
            @(negedge clk); bd_addr = word[15:0]; bd_wdata = v; bd_we = 1'b1;
            @(negedge clk); bd_we = 1'b0;
        end
    endtask

    integer i, t, nt, gm, gn, nk, ar_sp0;
    reg [255:0] gold [0:15];
    reg [255:0] ref1, poi1;

    initial begin
        ext_i = 0; ext_iv = 0;
        for (i = 0; i < 4096; i = i + 1) u_ram.mem[i] = 256'd0;

        repeat (10) @(negedge clk);
        rst = 0;
        repeat (10) @(negedge clk);

        // ============ 1. a real GEMM drained to DRAM: the golden tile ============
        $display("--- 1. GEMM, then DRAIN to DRAM: the reference ---");
        gm = 2; gn = 2; nk = 2; nt = gm*gn;

        for (i = 0; i < gm*4; i = i + 1)
            for (t = 0; t < nk*32; t = t + 1) A[i][t] = ($random(seed) & 7) - 4;
        for (t = 0; t < nk*32; t = t + 1)
            for (i = 0; i < gn*4; i = i + 1) B[t][i] = ($random(seed) & 7) - 4;
        for (i = 0; i < gm*4; i = i + 1)
            for (t = 0; t < nk; t = t + 1) SA[i][t] = 0;
        for (i = 0; i < gn*4; i = i + 1)
            for (t = 0; t < nk; t = t + 1) SB[i][t] = 0;

        fill_side(0, gm, nk);
        fill_side(1, gn, nk);
        send_gemm(gm, gn, nk);
        drain_at(nt, 40'd0);
        wait_drains(1, 400000);

        for (t = 0; t < nt; t = t + 1) begin
            bd_read(t);
            gold[t] = bd_rdata;
        end
        chk(gold[0] !== 256'd0, "the drain reached DRAM", 0);

        // ============ 2. the same tile DRAINED INTO aperture 0 ============
        $display("--- 2. DRAIN into MAG aperture 0 ---");
        send_gemm(gm, gn, nk);
        drain_at(nt, A_STG);
        wait_drains(2, 400000);

        chk(ax_aw_special == 0,
            "a staged drain put no special address on AXI", ax_aw_special);
        for (t = nt; t < 64; t = t + 1) begin
            bd_read(t);
            chk(bd_rdata === 256'd0, "an aperture drain touched no DRAM", t);
        end

        // ==== 3. THE DISCRIMINATED FILL: reference first, then DRAM poisoned
        // underneath, so only the store can still match it. ====
        $display("--- 3a. reference: fill both operands from DRAM word 0 ---");
        cl_fill(40'd0, 16'd1, 1'b0, 1'b1);
        cl_fill(40'd0, 16'd1, 1'b1, 1'b1);
        send_gemm(1, 1, 1);
        drain_at(1, 40'(W_REF * 32));
        wait_drains(3, 400000);
        bd_read(W_REF);
        ref1 = bd_rdata;
        chk(ref1 !== 256'd0, "the DRAM-filled GEMM produced a tile", 0);

        // Scale bytes live in [31:0] and are left alone, so the poisoned words
        // are still well-formed operands -- only their lane values change.
        $display("--- 3b. poison DRAM under the staged copy ---");
        for (t = 0; t < nt; t = t + 1)
            bd_write(t, gold[t] ^ {{7{32'h0F0F_0F0F}}, 32'h0});
        for (t = 0; t < nt; t = t + 1) begin
            bd_read(t);
            chk(bd_rdata !== gold[t], "DRAM now differs from the staged copy", t);
        end

        $display("--- 3c. the poison is VISIBLE through a DRAM fill ---");
        cl_fill(40'd0, 16'd1, 1'b0, 1'b1);
        cl_fill(40'd0, 16'd1, 1'b1, 1'b1);
        send_gemm(1, 1, 1);
        drain_at(1, 40'(W_POI * 32));
        wait_drains(4, 400000);
        bd_read(W_POI);
        poi1 = bd_rdata;
        // Without this the next check is vacuous: it would pass for a fill that
        // ignored its operands entirely.
        chk(poi1 !== ref1, "poisoned DRAM gives a DIFFERENT answer", 0);

        $display("--- 3d. the STAGED fill still equals the reference ---");
        cl_fill(A_STG, 16'd1, 1'b0, 1'b1);
        cl_fill(A_STG, 16'd1, 1'b1, 1'b1);
        send_gemm(1, 1, 1);
        drain_at(1, 40'(W_STG * 32));
        wait_drains(5, 400000);
        bd_read(W_STG);
        chk(bd_rdata === ref1,
            "a GEMM over STAGED operands equals the reference", 0);
        chk(bd_rdata !== poi1,
            "and is NOT what DRAM under the alias would have given", 0);
        chk(ax_ar_special == 0,
            "no fill ever put a special address on AXI", ax_ar_special);

        // Put the golden tile back: section 5 checks DRAM was not written.
        for (t = 0; t < nt; t = t + 1) bd_write(t, gold[t]);

        // ==== 3e. A DRAIN, not a FILL: a dropped fill wedges the cluster, so
        // the remote FILL is section 6, after everything it would take with it.
        $display("--- 3e. a DRAIN naming mesh 1's staging is not served here ---");
        i = ax_ar_any;
        send_gemm(1, 1, 1);
        drain_at(1, A_STG_M1);
        spin = 0;
        while ((mag_rem_wr == 0) && (spin < 40000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(mag_rem_wr > 0, "MAG saw the remote-staging write", mag_rem_wr);
        repeat (20000) @(negedge clk);
        chk(ax_aw_special == 0,
            "a remote staging drain put nothing on AXI", ax_aw_special);
        chk(ax_ar_any == i, "and issued no read either", ax_ar_any - i);

        // ==== 4. WRITE-path fault, BEFORE the read one: that drops a fill the
        // cluster waits on, and a wedged cluster issues no drain to test. ====
        $display("--- 4. DRAIN to aperture 3: it must not reach DRAM ---");
        chk(dbg_cluster[15:0] == 16'd6, "six drains retired so far",
            dbg_cluster[15:0]);
        // 1x1x1, the shape section 3 left the cluster in. Re-sweeping 2x2x2 here
        // reads L1 entries 3's single-entry fills overwrote, and drains x.
        send_gemm(1, 1, 1);
        drain_at(1, A_RSVD_W);
        spin = 0;
        while ((mag_rsvd_wr == 0) && (spin < 40000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(mag_rsvd_wr > 0, "MAG saw the reserved-aperture write", mag_rsvd_wr);
        repeat (20000) @(negedge clk);

        chk(ax_aw_special == 0,
            "a reserved-aperture drain put nothing on AXI", ax_aw_special);
        for (t = W_SPY; t < W_SPY + 8; t = t + 1) begin
            bd_read(t);
            chk(bd_rdata === 256'd0, "and it never aliased onto DRAM", t);
        end
        // mx_cluster_cu.v:886 retires a DRAIN when the last sub-tile has LEFT,
        // so a dropped write is invisible to the cluster: loud only in sim.
        chk(dbg_cluster[15:0] == 16'd7,
            "the cluster retired it regardless", dbg_cluster[15:0]);

        // ==== 5. ONE read drop per run: a dropped FILL parks the cluster in
        // S_FILL, so a second one is never issued. Aperture 3 is l2_tb s6's.
        $display("--- 5. FILL from mesh 1 staging: 1 ERROR expected, no fill ---");
        chk(dbg_cluster[47:32] == 16'd6, "six fills retired so far",
            dbg_cluster[47:32]);
        i = ax_ar_any;
        ar_sp0 = ax_ar_special;
        cl_fill(A_STG_M1, 16'd1, 1'b0, 1'b1);
        spin = 0;
        while ((mag_rem_rd == 0) && (spin < 40000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(mag_rem_rd > 0, "MAG saw the remote-staging read", mag_rem_rd);
        repeat (20000) @(negedge clk);

        chk(dbg_cluster[47:32] == 16'd6,
            "a remote aperture is DROPPED, not aliased", dbg_cluster[47:32]);
        chk(ax_ar_any == i, "and no AR was issued for it", ax_ar_any - i);
        chk(ax_ar_special == ar_sp0,
            "so no bit-39 address ever reached AXI", ax_ar_special);
        for (t = 0; t < nt; t = t + 1) begin
            bd_read(t);
            chk(bd_rdata === gold[t], "DRAM is untouched by the faulting fill", t);
        end

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    // COUNTED, not `#N`: a cycle count needs no timescale reasoning at all. The
    // `#12000000` it replaces did not fire on a run that printed far later.
    initial begin
        repeat (100000000) @(negedge clk);
        $display("  FAIL -- watchdog (cl=%h)", dbg_cluster);
        $finish;
    end

endmodule

`default_nettype wire
