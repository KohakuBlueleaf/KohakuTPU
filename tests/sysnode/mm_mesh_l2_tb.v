// THE L2 ADAPTER INSIDE A REAL MESH, reached by dispatched instructions.

// noc_l2_adapter_tb drives the module's ports directly. This one puts it in
// mm_mesh's cluster link and lets a GEMM and a DRAIN find it over two routers.

// The control plane is FRAMEWORK: CU_CTRL flits addressed to the cluster's own
// coordinate, terminated in the link, never reaching mx_cluster_cu.

// ADDR34=0: the producers now zero-extend their 34-bit address into the 40-bit
// NOC_MEM_ADDR field, so the old map really is the bottom corner of the new one.

`default_nettype none
`timescale 1ns/1ps

module mm_mesh_l2_tb;
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4;
    localparam MEMP = 1, NCH = 1;

    localparam [3:0] T_MEM_WR_ACK = 4'h3, T_CU_INST = 4'h5, T_CU_SIGNAL = 4'h6;
    localparam [3:0] T_CU_CTRL = 4'h7, T_CU_DATA = 4'h8;

    // The bench is the agent on r11's local port; the cluster is at (2,1).
    localparam CX = 1, CY = 1;
    localparam LX = 2, LY = 1;

    localparam integer SBIAS = 20;
    localparam [7:0] ANCHOR = 8'd40;

    // 1024 lines of 32 bytes = 32 KB, so the window is 15 address bits.
    localparam integer L2_LINES = 1024;
    localparam integer L2_BITS  = 15;
    localparam [39:0]  L2_BASE  = 40'h00_0001_0000;    // byte 65536, word 2048
    localparam [7:0]   CB       = 8'hE0;

    localparam [33:0] A_GOLD = 34'h0000;               // drained to DRAM
    localparam integer W_L2  = 2048;                   // the window, in words

    // MAG staging is an ADDRESS, not a range: [39]=1, mesh 0, aperture 0. The
    // reserved aperture 3 must fault rather than alias onto DRAM.
    localparam [39:0] A_MAGL2 = 40'h80_0000_0000;
    localparam [39:0] A_RSVD  = 40'h83_0000_0000;
    localparam integer W_R1 = 1024, W_R2 = 1025;   // the two compared results

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
              .MODEL(1),
              .L2_CU(1), .L2_CU_DEPTH(L2_LINES), .L2_CU_BASE(L2_BASE),
              .L2_CU_BITS(L2_BITS), .L2_CU_A34(0),
              // The SHIPPED 4-bank shape, shortened to 1,024 entries so the
              // simulator is not asked to model 2 MB of URAM.
              .L2_MAG(1), .L2_MAG_BANKS(4), .L2_MAG_ENTRIES(1024),
              .L2_MAG_MESH(0)) dut (
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
                if (errors < 20) begin
                    $display("  FAIL %0s [%0d]", what, where);
                end
            end
        end
    endtask

    // ================================================ the bench as the agent
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

    task cl_inst(input [255:0] p);
        begin send_flit(LX[3:0], LY[3:0], T_CU_INST, 8'h40, 1'b0, p); end
    endtask

    // A framework control message for the LINK, addressed to the unit behind it.
    task l2_ctrl(input [7:0] txn, input [7:0] op, input [7:0] idx,
                 input [63:0] val);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 8]  = op;
            p[247 -: 8]  = idx;
            p[239 -: 64] = val;
            send_flit(LX[3:0], LY[3:0], T_CU_CTRL, txn, 1'b1, p);
        end
    endtask

    // ================================================ what comes back
    integer sig_cl = 0, n_ctrl = 0;
    reg [63:0] ctrl_val;
    reg [7:0]  ctrl_idx;

    wire [3:0] o_type = ext_o[FW-4*PW-1 -: 4];

    always @(posedge clk) if (!rst && ext_ov) begin
        if (o_type == T_CU_SIGNAL) begin
            sig_cl = sig_cl + 1;
        end
        else if (o_type == T_CU_CTRL) begin
            n_ctrl   = n_ctrl + 1;
            ctrl_idx = ext_o[247 -: 8];
            ctrl_val = ext_o[239 -: 64];
        end
    end

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
            for (ii = 0; ii < 4; ii = ii + 1) begin
                for (kk = 0; kk < 8; kk = kk + 1) begin
                    word_a[255 - (ii*8+kk)*7 -: 7] = A[row0+ii][kb*32 + w*8 + kk][6:0];
                end
            end
        end
    endfunction

    function [255:0] word_b(input integer col0, input integer kb, input integer w);
        integer jj, kk;
        begin
            word_b = 256'd0;
            for (kk = 0; kk < 8; kk = kk + 1) begin
                for (jj = 0; jj < 4; jj = jj + 1) begin
                    word_b[255 - (kk*4+jj)*7 -: 7] = B[kb*32 + w*8 + kk][col0+jj][6:0];
                end
            end
        end
    endfunction

    function [255:0] with_scales(input [255:0] w, input integer lane0,
                                 input integer kb, input integer side);
        integer ii;
        begin
            with_scales = w;
            for (ii = 0; ii < 4; ii = ii + 1) begin
                with_scales[31 - ii*8 -: 8] =
                    ((side == 0 ? SA[lane0+ii][kb] : SB[lane0+ii][kb]) + SBIAS) << 3;
            end
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
            for (e = 0; e < ne; e = e + 1) begin
                for (w = 0; w < 4; w = w + 1) begin
                    p = (side == 0) ? word_a((e/nk)*4, e % nk, w)
                                    : word_b((e/nk)*4, e % nk, w);
                    if (w == 0) begin
                        p = with_scales(p, (e/nk)*4, e % nk, side);
                    end
                    send_flit(LX[3:0], LY[3:0], T_CU_DATA, 8'h00,
                              (e == ne-1) && (w == 3), p);
                end
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

    // DRAIN to MEMORY: dnode stays 0, so the sub-tiles leave as MEM_WR_REQ at
    // `word * 32` -- which is what the adapter is in the link to intercept.
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

    task drain_mem(input integer nt, input integer word);
        begin drain_at(nt, 40'(word * 32)); end
    endtask

    // op 1 FILL. `preq` says the operand is ALREADY quantised, which is what
    // staging holds -- MAG serves it verbatim instead of converting.
    task cl_fill(input [39:0] addr, input [15:0] n, input sel, input preq);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 4]  = 4'd1;
            p[251 -: 34] = addr[33:0];
            p[68  -: 6]  = addr[39:34];
            p[217 -: 16] = n;
            p[201]       = sel;
            // [141], NOT [200] -- 200 is the GEMM's `acc`. With preq low the
            // fetch quantises, and a quantising fetch is never staged.
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

    integer i, t, nt, gm, gn, nk;
    reg [255:0] gold [0:15];
    reg [255:0] ref1;

    initial begin
        ext_i = 0; ext_iv = 0;
        for (i = 0; i < 4096; i = i + 1) begin
            u_ram.mem[i] = 256'd0;
        end

        repeat (10) @(negedge clk);
        rst = 0;
        repeat (10) @(negedge clk);

        // ============ 1. the framework control plane, over two routers ============
        $display("--- 1. CU_CTRL reaches the adapter and not the cluster ---");
        l2_ctrl(8'h70, 8'd0, CB + 8'd0, 64'd0);
        repeat (200) @(negedge clk);
        chk(n_ctrl == 1, "the adapter answered a CU_CTRL read", n_ctrl);
        chk(ctrl_idx == (CB + 8'd0), "echoing its index", 0);
        chk(ctrl_val[63:48] == 16'h0002, "CAPS names the staging slot", 0);
        chk(ctrl_val[31:0] == L2_LINES, "and the store it holds", 0);
        chk(sig_cl == 0, "the cluster saw nothing to retire", sig_cl);

        l2_ctrl(8'h71, 8'd1, CB + 8'd1, {24'd0, L2_BASE});
        l2_ctrl(8'h72, 8'd1, CB + 8'd2, 64'd1);
        repeat (200) @(negedge clk);
        chk(n_ctrl == 3, "the window was programmed and enabled", n_ctrl);

        // ============ 2. a real GEMM, drained to DRAM: the golden tile ============
        $display("--- 2. GEMM, then DRAIN to DRAM ---");
        gm = 2; gn = 2; nk = 2; nt = gm*gn;

        for (i = 0; i < gm*4; i = i + 1) begin
            for (t = 0; t < nk*32; t = t + 1) begin
                A[i][t] = ($random(seed) & 7) - 4;
            end
        end
        for (t = 0; t < nk*32; t = t + 1) begin
            for (i = 0; i < gn*4; i = i + 1) begin
                B[t][i] = ($random(seed) & 7) - 4;
            end
        end
        for (i = 0; i < gm*4; i = i + 1) begin
            for (t = 0; t < nk; t = t + 1) begin
                SA[i][t] = 0;
            end
        end
        for (i = 0; i < gn*4; i = i + 1) begin
            for (t = 0; t < nk; t = t + 1) begin
                SB[i][t] = 0;
            end
        end

        fill_side(0, gm, nk);
        fill_side(1, gn, nk);
        send_gemm(gm, gn, nk);
        drain_mem(nt, 0);
        wait_drains(1, 400000);

        for (t = 0; t < nt; t = t + 1) begin
            @(negedge clk); bd_addr = (A_GOLD >> 5) + t[15:0];
            @(negedge clk); @(negedge clk);
            gold[t] = bd_rdata;
        end
        chk(gold[0] !== 256'd0, "the drain reached DRAM", 0);

        // Every flit of it is a real MEM_WR_REQ from a real cluster: nothing
        // may reach DRAM, and the cluster must not notice.
        $display("--- 3. GEMM, then DRAIN into the L2 window ---");
        send_gemm(gm, gn, nk);
        drain_mem(nt, W_L2);
        wait_drains(2, 400000);

        for (t = 0; t < nt; t = t + 1) begin
            @(negedge clk); bd_addr = W_L2[15:0] + t[15:0];
            @(negedge clk); @(negedge clk);
            chk(bd_rdata === 256'd0, "the staged drain never reached DRAM", t);
        end
        chk(dbg_cluster[15:0] == 16'd2, "and the cluster retired it anyway",
            dbg_cluster[15:0]);

        // The adapter's own account of what it did, read as a framework
        // register rather than inferred.
        n_ctrl = 0;
        l2_ctrl(8'h73, 8'd0, CB + 8'd3, 64'd0);
        repeat (200) @(negedge clk);
        chk(n_ctrl == 1, "the counters answered", n_ctrl);
        chk(ctrl_val[63:32] > 32'd0, "the adapter served the write acks", 0);
        chk(ctrl_val[31:0] == 32'd0, "and forwarded nothing in-window", 0);

        // ============ 4. disabled, the same drain goes to DRAM ============
        $display("--- 4. disable the window: the drain lands in DRAM again ---");
        l2_ctrl(8'h74, 8'd1, CB + 8'd2, 64'd0);
        repeat (200) @(negedge clk);
        send_gemm(gm, gn, nk);
        drain_mem(nt, W_L2);
        wait_drains(3, 400000);

        for (t = 0; t < nt; t = t + 1) begin
            @(negedge clk); bd_addr = W_L2[15:0] + t[15:0];
            @(negedge clk); @(negedge clk);
            chk(bd_rdata === gold[t], "the same tile, now in DRAM", t);
        end

        // ============ 5. MAG staging: aperture 0, drained into and filled from ====
        $display("--- 5. DRAIN into MAG aperture 0, then FILL back out of it ---");
        send_gemm(gm, gn, nk);
        drain_at(nt, A_MAGL2);
        wait_drains(4, 400000);

        for (t = 0; t < 64; t = t + 1) begin
            @(negedge clk); bd_addr = t[15:0];
            @(negedge clk); @(negedge clk);
            if (t >= nt) begin
                chk(bd_rdata === 256'd0, "an aperture drain touched no DRAM", t);
            end
        end

        // The fill can only complete if MAG answered it from staging: nothing
        // ever wrote DRAM at an address with bit 39 set.
        cl_fill(A_MAGL2, 16'd1, 1'b0, 1'b1);
        spin = 0;
        while ((dbg_cluster[47:32] < 16'd1) && (spin < 200000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(spin < 200000, "a FILL from aperture 0 completed", 0);
        chk(dbg_cluster[47:32] == 16'd1, "exactly one fill retired",
            dbg_cluster[47:32]);

        // The SAME tile sits at DRAM word 0 and at aperture 0, so filling both
        // operands from each and draining the GEMM must agree bit for bit.
        $display("--- 5b. fill from DRAM vs fill from aperture 0 ---");
        cl_fill(40'd0, 16'd1, 1'b0, 1'b1);
        cl_fill(40'd0, 16'd1, 1'b1, 1'b1);
        send_gemm(1, 1, 1);
        drain_at(1, 40'(W_R1 * 32));
        wait_drains(5, 400000);

        @(negedge clk); bd_addr = W_R1[15:0];
        @(negedge clk); @(negedge clk);
        ref1 = bd_rdata;
        chk(ref1 !== 256'd0, "the DRAM-filled GEMM produced a tile", 0);

        // POISON, and it is the whole point of this section: axi_ram truncates
        // the index, so 0x80_0000_0000 aliases onto word 0 if bit 39 is ignored.
        for (t = 0; t < 4; t = t + 1) begin
            @(negedge clk);
            bd_addr = t[15:0]; bd_wdata = {8{32'hDEAD_0000 + t[31:0]}};
            bd_we = 1'b1;
            @(negedge clk); bd_we = 1'b0;
        end

        cl_fill(A_MAGL2, 16'd1, 1'b0, 1'b1);
        cl_fill(A_MAGL2, 16'd1, 1'b1, 1'b1);
        send_gemm(1, 1, 1);
        drain_at(1, 40'(W_R2 * 32));
        wait_drains(6, 400000);

        @(negedge clk); bd_addr = W_R2[15:0];
        @(negedge clk); @(negedge clk);
        chk(bd_rdata === ref1,
            "a GEMM over staged operands equals the DRAM reference", 0);
        chk(dbg_cluster[47:32] == 16'd5, "five fills retired in all",
            dbg_cluster[47:32]);

        // ============ 6. a reserved aperture faults rather than aliasing ====
        $display("--- 6. aperture 3 is reserved: 1 ERROR expected, no fill ---");
        cl_fill(A_RSVD, 16'd1, 1'b0, 1'b1);
        repeat (20000) @(negedge clk);
        chk(dbg_cluster[47:32] == 16'd5,
            "a reserved aperture is DROPPED, not answered", dbg_cluster[47:32]);
        for (t = 0; t < 4; t = t + 1) begin
            @(negedge clk); bd_addr = t[15:0];
            @(negedge clk); @(negedge clk);
            chk(bd_rdata === {8{32'hDEAD_0000 + t[31:0]}},
                "and it never aliased onto DRAM", t);
        end

        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #12000000;
        $display("  FAIL -- watchdog (ctrl=%0d cl=%h)", n_ctrl, dbg_cluster);
        $finish;
    end

endmodule

`default_nettype wire
