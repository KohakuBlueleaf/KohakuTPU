// CROSS-MESH STAGING, end to end: a memory WRITE naming mesh 1's aperture 0 is
// encapsulated by mesh 0's MAG, crosses the link, and lands in mesh 1's store.

// PUSH, NOT PULL. A remote write is answered locally, so it adds no cross-link
// request/response dependency; a remote READ would. See l2-landing.md.

// The bench is the NoC: no routers here, so what MAG emits for its own memory
// node is fed back to it and what it emits for (1,1) is captured.

// MESH 1's DRAM IS ALL POISON. A fill that quietly aliased onto DRAM instead of
// reading the store returns poison, so the compare cannot pass by accident.

`timescale 1ns / 1ps
`default_nettype none

module interlink_stage_tb;
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4, MW = 512;
    localparam LW = 288, UW = 96;

    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_RD_RESP = 4'h2, T_MEM_WR_DATA = 4'h4;

    // MAG's memory node, and the coordinate the bench answers to.
    localparam [3:0] MX = 4'd0, MY = 4'd1;
    localparam [3:0] CX = 4'd1, CY = 4'd1;

    // [39] special, [38] rsvd, [37:36] mesh, [35:32] aperture. Mesh 1 is
    // +0x10_, NOT +0x01_: 0x81_ would be mesh 0's aperture 1.
    localparam [39:0] A_STG_M1 = 40'h90_0000_0000;
    localparam [255:0] POISON  = {8{32'hDEAD_BEEF}};

    reg clk = 0, resetn = 0, dclk = 0;
    always begin
        #2   clk  = ~clk;
    end
    always begin
        #1.7 dclk = ~dclk;
    end

    wire [LW-1:0] o0_d [0:1], o1_d [0:1];
    wire [UW-1:0] o0_u [0:1], o1_u [0:1];
    wire [1:0]    o0_l, o0_v, o1_l, o1_v;

    wire [IDW-1:0]  m_awid [0:1], m_arid [0:1], m_bid [0:1], m_rid [0:1];
    wire [AW-1:0]   m_awaddr[0:1], m_araddr[0:1];
    wire [7:0]      m_awlen[0:1], m_arlen[0:1];
    wire [2:0]      m_awsize[0:1], m_arsize[0:1];
    wire [1:0]      m_awburst[0:1], m_arburst[0:1], m_bresp[0:1], m_rresp[0:1];
    wire [1:0]      m_awvalid, m_awready, m_arvalid, m_arready;
    wire [MW-1:0]   m_wdata[0:1], m_rdata[0:1];
    wire [MW/8-1:0] m_wstrb[0:1];
    wire [1:0]      m_wlast, m_wvalid, m_wready;
    wire [1:0]      m_bvalid, m_bready, m_rlast, m_rvalid, m_rready;

    reg  [FW-1:0] mi_data [0:1];
    reg  [1:0]    mi_valid;
    wire [1:0]    mi_busy;
    wire [FW-1:0] mo_data [0:1];
    wire [1:0]    mo_valid;

    genvar g;
    generate for (g = 0; g < 2; g = g + 1) begin : mesh
        sysnode #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW),
                 .ID_W(IDW), .PORTS(1), .MEM_X(MX), .MEM_Y(MY),
                 .GRID_LO(1), .GRID_HI(1), .STAGE_FLITS(128),
                 .ILINK(1), .MESH_ID(g), .LINK_W(LW), .TUSER_W(UW), .MW(MW),
                 // Shortened so the simulator is not asked to model 2 MB.
                 .STAGE(1), .STAGE_BANKS(4), .STAGE_ENTRIES(1024),
                 // -d MM_L2_PORT proves the SAME cross-mesh landing when the
                 // store sits on MAG's converged path instead of the mem port.
`ifdef MM_L2_PORT
                 .STAGE_AT_PORT(1)
`else
                 .STAGE_AT_PORT(0)
`endif
                 ) u (
            .clk(clk), .resetn(resetn),
            .dram_aclk(dclk), .dram_aresetn(resetn),
            .sm_awid({IDW{1'b0}}), .sm_awaddr({AW{1'b0}}), .sm_awlen(8'd0),
            .sm_awvalid(1'b0), .sm_awready(),
            .sm_wdata({DW{1'b0}}), .sm_wstrb({(DW/8){1'b0}}), .sm_wlast(1'b0),
            .sm_wvalid(1'b0), .sm_wready(),
            .sm_bid(), .sm_bresp(), .sm_bvalid(), .sm_bready(1'b1),
            .sm_arid({IDW{1'b0}}), .sm_araddr({AW{1'b0}}), .sm_arlen(8'd0),
            .sm_arvalid(1'b0), .sm_arready(),
            .sm_rid(), .sm_rdata(), .sm_rresp(), .sm_rlast(), .sm_rvalid(),
            .sm_rready(1'b1),

            .sc_awid({IDW{1'b0}}), .sc_awaddr(32'd0), .sc_awlen(8'd0),
            .sc_awvalid(1'b0), .sc_awready(),
            .sc_wdata(64'd0), .sc_wstrb(8'hFF), .sc_wlast(1'b1),
            .sc_wvalid(1'b0), .sc_wready(),
            .sc_bid(), .sc_bresp(), .sc_bvalid(), .sc_bready(1'b1),
            .sc_arid({IDW{1'b0}}), .sc_araddr(32'd0), .sc_arlen(8'd0),
            .sc_arvalid(1'b0), .sc_arready(),
            .sc_rid(), .sc_rdata(), .sc_rresp(), .sc_rlast(), .sc_rvalid(),
            .sc_rready(1'b1),

            .mem_in_data(mi_data[g]), .mem_in_valid(mi_valid[g]),
            .mem_in_busy(mi_busy[g]),
            .mem_out_data(mo_data[g]), .mem_out_valid(mo_valid[g]),
            .mem_out_busy(1'b0),
            .mem_rd_count(), .mem_wr_count(),
        .pe_halt_req(1'b0), .pe_status(), .pe_busy(),
            .mv_busy(), .mv_fault(), .mv_done(),

            .dram_awid(m_awid[g]), .dram_awaddr(m_awaddr[g]), .dram_awlen(m_awlen[g]),
            .dram_awsize(m_awsize[g]), .dram_awburst(m_awburst[g]),
            .dram_awvalid(m_awvalid[g]), .dram_awready(m_awready[g]),
            .dram_wdata(m_wdata[g]), .dram_wstrb(m_wstrb[g]), .dram_wlast(m_wlast[g]),
            .dram_wvalid(m_wvalid[g]), .dram_wready(m_wready[g]),
            .dram_bid(m_bid[g]), .dram_bresp(m_bresp[g]), .dram_bvalid(m_bvalid[g]),
            .dram_bready(m_bready[g]),
            .dram_arid(m_arid[g]), .dram_araddr(m_araddr[g]), .dram_arlen(m_arlen[g]),
            .dram_arsize(m_arsize[g]), .dram_arburst(m_arburst[g]),
            .dram_arvalid(m_arvalid[g]), .dram_arready(m_arready[g]),
            .dram_rid(m_rid[g]), .dram_rdata(m_rdata[g]), .dram_rresp(m_rresp[g]),
            .dram_rlast(m_rlast[g]), .dram_rvalid(m_rvalid[g]),
            .dram_rready(m_rready[g]),

            // mesh0 reaches mesh1 by its UP link, as on the SLR chain.
            .link0_out_tdata(o0_d[g]), .link0_out_tuser(o0_u[g]),
            .link0_out_tlast(o0_l[g]), .link0_out_tvalid(o0_v[g]),
            .link0_out_tready(1'b1),
            .link0_in_tdata(g == 1 ? o1_d[0] : {LW{1'b0}}),
            .link0_in_tuser(g == 1 ? o1_u[0] : {UW{1'b0}}),
            .link0_in_tlast(g == 1 ? o1_l[0] : 1'b0),
            .link0_in_tvalid(g == 1 ? o1_v[0] : 1'b0),
            .link0_in_tready(),
            .link1_out_tdata(o1_d[g]), .link1_out_tuser(o1_u[g]),
            .link1_out_tlast(o1_l[g]), .link1_out_tvalid(o1_v[g]),
            .link1_out_tready(1'b1),
            .link1_in_tdata(g == 0 ? o0_d[1] : {LW{1'b0}}),
            .link1_in_tuser(g == 0 ? o0_u[1] : {UW{1'b0}}),
            .link1_in_tlast(g == 0 ? o0_l[1] : 1'b0),
            .link1_in_tvalid(g == 0 ? o0_v[1] : 1'b0),
            .link1_in_tready()
        );

        axi_ram #(.DATA_W(MW), .ADDR_W(AW), .ID_W(IDW), .WORDS(2048),
                  .PORTS(1)) ram (
            .clk(dclk), .resetn(resetn),
            .s_awid(m_awid[g]), .s_awaddr(m_awaddr[g]), .s_awlen(m_awlen[g]),
            .s_awsize(m_awsize[g]), .s_awburst(m_awburst[g]),
            .s_awvalid(m_awvalid[g]), .s_awready(m_awready[g]),
            .s_wdata(m_wdata[g]), .s_wstrb(m_wstrb[g]), .s_wlast(m_wlast[g]),
            .s_wvalid(m_wvalid[g]), .s_wready(m_wready[g]),
            .s_bid(m_bid[g]), .s_bresp(m_bresp[g]), .s_bvalid(m_bvalid[g]),
            .s_bready(m_bready[g]),
            .s_arid(m_arid[g]), .s_araddr(m_araddr[g]), .s_arlen(m_arlen[g]),
            .s_arsize(m_arsize[g]), .s_arburst(m_arburst[g]),
            .s_arvalid(m_arvalid[g]), .s_arready(m_arready[g]),
            .s_rid(m_rid[g]), .s_rdata(m_rdata[g]), .s_rresp(m_rresp[g]),
            .s_rlast(m_rlast[g]), .s_rvalid(m_rvalid[g]),
            .s_rready(m_rready[g]),
            .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({MW{1'b0}}), .bd_rdata()
        );
    end endgenerate

    integer errors = 0, checks = 0, i, t, spin;

    task chk(input cond, input [255:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 12) begin
                    $display("  FAIL %0s [%0d]", what, where);
                end
            end
        end
    endtask

    // No address with bit 39 set may reach either DRAM: the slave ignores the
    // top bits, so an alias lands silently rather than erroring.
    integer ax_special = 0;
    // ITS OWN COUNTER. Sharing `i` with the stimulus corrupted that loop every
    // posedge: `push` waits on a clock, so i came back as 2 and never reached 4.
    integer ax;
    always @(posedge clk) if (resetn) begin
        for (ax = 0; ax < 2; ax = ax + 1) begin
            if (
                (m_awvalid[ax] && m_awaddr[ax][39])
                || (m_arvalid[ax] && m_araddr[ax][39])
            ) begin
                ax_special = ax_special + 1;
                if (ax_special < 5) begin
                    $display("%0t ERROR bench: mesh %0d put a special aperture on AXI",
                             $time, ax);
                end
            end
        end
    end

    // ================================================ the bench as the NoC
    // Two sources per mesh. Separate queues because one is written from an
    // always block and one from a task, and a shared pointer cannot serve both.
    reg [FW-1:0] lq [0:1][0:63];
    reg [FW-1:0] bq [0:1][0:63];
    integer lq_w [0:1], lq_r [0:1], bq_w [0:1], bq_r [0:1];

    reg [FW-1:0] rsp [0:7];
    integer      nrsp;

    wire [3:0] o_dx [0:1], o_dy [0:1];
    generate for (g = 0; g < 2; g = g + 1) begin : obs
        assign o_dx[g] = mo_data[g][FW-1 -: 4];
        assign o_dy[g] = mo_data[g][FW-5 -: 4];
    end endgenerate

    integer k;
    always @(posedge clk) if (resetn) begin
        for (k = 0; k < 2; k = k + 1) begin
            if (mo_valid[k]) begin
                if (o_dx[k] == MX && o_dy[k] == MY) begin
                    lq[k][lq_w[k] % 64] <= mo_data[k];
                    lq_w[k] <= lq_w[k] + 1;
                end else if (o_dx[k] == CX && o_dy[k] == CY) begin
                    rsp[nrsp % 8] <= mo_data[k];
                    nrsp <= nrsp + 1;
                end
            end
        end
    end

    // Injected traffic first: it is already in flight and the far mesh is
    // waiting on it, while the bench can always be made to wait.
    always @(posedge clk) begin
        if (!resetn) begin
            mi_valid <= 2'd0;
        end else begin
            for (k = 0; k < 2; k = k + 1) begin
                if (!mi_valid[k] || !mi_busy[k]) begin
                    if (lq_r[k] != lq_w[k]) begin
                        mi_data[k]  <= lq[k][lq_r[k] % 64];
                        mi_valid[k] <= 1'b1;
                        lq_r[k]     <= lq_r[k] + 1;
                    end else if (bq_r[k] != bq_w[k]) begin
                        mi_data[k]  <= bq[k][bq_r[k] % 64];
                        mi_valid[k] <= 1'b1;
                        bq_r[k]     <= bq_r[k] + 1;
                    end
                    else begin
                        mi_valid[k] <= 1'b0;
                    end
                end
            end
        end
    end

    task push(input integer m, input [3:0] ty, input [7:0] txn, input lst,
              input [2:0] rsv, input [255:0] pay);
        begin
            bq[m][bq_w[m] % 64] = {MX, MY, CX, CY, ty, txn, lst, rsv, pay};
            bq_w[m] = bq_w[m] + 1;
            @(negedge clk);
        end
    endtask

    // COUNT EVERY FIELD TO 256: a short concatenation zero-extends on the LEFT,
    // so 8 missing bits shifted the address right by 8 and nothing errored.
    function [255:0] wr_desc(input [39:0] a, input [7:0] beats);
        begin wr_desc = {a, (beats - 8'd1), 208'd0}; end
    endfunction
    // flags [207:200] bit 6 STREAM bit 4 QUANT, count [199:192], peer [191:168],
    // nd [167:166], ew [165:158] where 0 keeps the legacy four words.
    function [255:0] rd_desc(input [39:0] a);
        begin rd_desc = {a, 8'd0, 8'h40, 8'd1, 24'd0, 2'd0, 8'd0, 158'd0}; end
    endfunction

    reg [255:0] payload [0:3];

    initial begin
        for (k = 0; k < 2; k = k + 1) begin
            lq_w[k] = 0; lq_r[k] = 0; bq_w[k] = 0; bq_r[k] = 0;
        end
        nrsp = 0;
        for (i = 0; i < 2048; i = i + 1) begin
            mesh[0].ram.mem[i] = {MW{1'b0}};
            // Mesh 1's DRAM is poison everywhere, so an aliased fill is visible
            // whatever address it truncates to.
            mesh[1].ram.mem[i] = {2{POISON}};
        end
        for (i = 0; i < 4; i = i + 1) begin
            payload[i] = {8{32'hC0DE_0000 | i[31:0]}};
        end

        repeat (20) @(negedge clk);
        resetn = 1'b1;
        repeat (60) @(negedge clk);

        // txn carries {fin_y, fin_x} -- where the FAR mesh delivers -- and rsvd
        // is {remote, mesh}. Both are on the descriptor AND every data flit.
        $display("--- 1. a remote DRAIN to mesh 1's staging ---");
        push(0, T_MEM_WR_REQ, {MY, MX}, 1'b0, 3'b101, wr_desc(A_STG_M1, 8'd4));
        for (i = 0; i < 4; i = i + 1) begin
            push(0, T_MEM_WR_DATA, {MY, MX}, (i == 3), 3'b101, payload[i]);
        end

        repeat (4000) @(negedge clk);
        chk(ax_special == 0, "no special aperture reached either DRAM",
            ax_special);
        for (i = 0; i < 2048; i = i + 1) begin
            if (mesh[0].ram.mem[i] !== {MW{1'b0}}) begin
                chk(1'b0, "the remote write did NOT land in mesh 0's DRAM", i);
                i = 2048;
            end
        end
        chk(mesh[1].ram.mem[0] === {2{POISON}},
            "and mesh 1's DRAM is still poison", 0);

        // ==== 2. mesh 1 fills locally from the same address ====
        $display("--- 2. mesh 1 FILLs from its own staging ---");
        nrsp = 0;
        push(1, T_MEM_RD_REQ, 8'h20, 1'b1, 3'b000, rd_desc(A_STG_M1));

        spin = 0;
        while ((nrsp < 4) && (spin < 200000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(nrsp >= 4, "four response words came back", nrsp);

        // The two spare header bits carry the word within the entry, so the
        // compare does not depend on arrival order.
        for (i = 0; i < 4; i = i + 1) begin
            t = rsp[i][257:256];
            chk(rsp[i][FW-4*PW-1 -: 4] === T_MEM_RD_RESP,
                "and each is a MEM_RD_RESP", i);
            chk(rsp[i][255:0] === payload[t],
                "the word mesh 0 wrote is the word mesh 1 read", i);
            chk(rsp[i][255:0] !== POISON,
                "and it is NOT what DRAM under the alias holds", i);
        end
        chk(ax_special == 0, "the fill put nothing special on AXI", ax_special);

        if (errors == 0) begin
            $display("  PASS interlink_stage_tb: %0d checks", checks);
        end
        else begin
            $display("  FAIL interlink_stage_tb: %0d errors, %0d checks",
                     errors, checks);
        end
        $finish;
    end

    // Counted, not `#N`: a delay past 32 bits at 1 ps precision never fires.
    initial begin
        repeat (2000000) @(negedge clk);
        $display("  FAIL interlink_stage_tb: watchdog (nrsp=%0d)", nrsp);
        $finish;
    end
endmodule

`default_nettype wire
