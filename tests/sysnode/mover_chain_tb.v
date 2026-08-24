// The mover across the SLR chain, at 1, 2 and 4 MAGs, driven the way software
// drives it: descriptors written to S_AXI_CTRL, not to the module's registers.

// The chain is a LINE, 0 - 1 - 3 - 2, so mesh0 to mesh2 is THREE hops and
// transits two MAGs. `cat()` below is the only place that order appears.

// A remote write is POSTED -- mag_ilink answers it locally and at once -- so
// stat_busy falling does NOT mean the bytes landed. The bench times both.

// Verified at every depth: the bytes arrive, a TRANSIT mesh's DRAM is untouched,
// the source is unmodified, and every burst is aligned and inside 4 KB.

`timescale 1ns / 1ps
`default_nettype none

module mover_chain_core #(
    parameter integer NMESH = 4,
    parameter integer SRCP  = 0,        // chain position of the source
    parameter integer DSTP  = 3,        // chain position of the destination
    parameter integer NW    = 64        // 32-byte words to move
)();
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4, MW = 512;
    localparam LW = 288, UW = 96;
    localparam integer RAMW = 2048;     // 512-bit words per mesh = 128 KB
    // 64 words take 1,722 cycles at one MAG, so this is 23x margin. Generous
    // limits turned a broken remote path into an hour of simulation, not a FAIL.
    localparam integer SPIN_MAX = 40000;

    reg clk = 0, resetn = 0, dclk = 0;
    always begin
        #2   clk  = ~clk;
    end
    always begin
        #1.7 dclk = ~dclk;
    end

    integer cyc = 0;
    always @(posedge clk) begin
        cyc <= cyc + 1;
    end

    // Chain position -> mesh id. The SLR order, and nothing else knows it.
    function integer cat(input integer p);
        cat = (p == 0) ? 0 : (p == 1) ? 1 : (p == 2) ? 3 : 2;
    endfunction

    localparam integer SRCM = 0;        // set below from cat(SRCP)
    wire [1:0] dst_mesh = cat(DSTP);

    reg  [31:0]  bd_a;
    wire [255:0] bd_rd [0:3];

    wire [LW-1:0] o0_d [0:3], o1_d [0:3];
    wire [UW-1:0] o0_u [0:3], o1_u [0:3];
    wire [3:0]    o0_l, o0_v, o1_l, o1_v;

    reg  [31:0] sc_awaddr [0:3];
    reg  [3:0]  sc_awvalid, sc_wvalid;
    reg  [63:0] sc_wdata  [0:3];
    wire [3:0]  sc_awready, sc_wready, sc_bvalid;

    wire [IDW-1:0]  m_awid  [0:3], m_arid [0:3], m_bid [0:3], m_rid [0:3];
    wire [AW-1:0]   m_awaddr[0:3], m_araddr[0:3];
    wire [7:0]      m_awlen [0:3], m_arlen [0:3];
    wire [2:0]      m_awsize[0:3], m_arsize[0:3];
    wire [1:0]      m_awburst[0:3], m_arburst[0:3], m_bresp[0:3], m_rresp[0:3];
    wire [3:0]      m_awvalid, m_awready, m_arvalid, m_arready;
    wire [MW-1:0]   m_wdata [0:3], m_rdata[0:3];
    wire [MW/8-1:0] m_wstrb [0:3];
    wire [3:0]      m_wlast, m_wvalid, m_wready;
    wire [3:0]      m_bvalid, m_bready, m_rlast, m_rvalid, m_rready;

    genvar g;
    generate for (g = 0; g < NMESH; g = g + 1) begin : mesh
        localparam integer MID  = cat(g);
        localparam integer HAS0 = (g != 0) ? 1 : 0;
        localparam integer HAS1 = (g != NMESH - 1) ? 1 : 0;

        // -d MV_L2 swaps in the SAME mesh built with MAG staging, so the mover
        // has a store to address. The port lists are identical.
`ifdef MV_L2
        ktpu_min_1m_l2 #(.MESH_ID(MID), .MODEL(1), .MW(MW)) u (
`else
        ktpu_min_1m #(.MESH_ID(MID), .MODEL(1), .MW(MW)) u (
`endif
            .axi_aclk(clk), .axi_aresetn(resetn),
            // MAG runs on noc_clk at MAG_CDC=0, so leaving these unconnected
            // left the mover unclocked -- it reported stat_fault === X.
            .noc_clk(clk), .mat_clk(clk), .vec_clk(clk),
            .dram_aclk(dclk), .dram_aresetn(resetn),
            .S_AXI_MEM_awid({IDW{1'b0}}), .S_AXI_MEM_awaddr({AW{1'b0}}),
            .S_AXI_MEM_awlen(8'd0), .S_AXI_MEM_awvalid(1'b0),
            .S_AXI_MEM_awready(),
            .S_AXI_MEM_wdata({DW{1'b0}}), .S_AXI_MEM_wstrb({(DW/8){1'b0}}),
            .S_AXI_MEM_wlast(1'b0), .S_AXI_MEM_wvalid(1'b0),
            .S_AXI_MEM_wready(),
            .S_AXI_MEM_bid(), .S_AXI_MEM_bresp(), .S_AXI_MEM_bvalid(),
            .S_AXI_MEM_bready(1'b1),
            .S_AXI_MEM_arid({IDW{1'b0}}), .S_AXI_MEM_araddr({AW{1'b0}}),
            .S_AXI_MEM_arlen(8'd0), .S_AXI_MEM_arvalid(1'b0),
            .S_AXI_MEM_arready(),
            .S_AXI_MEM_rid(), .S_AXI_MEM_rdata(), .S_AXI_MEM_rresp(),
            .S_AXI_MEM_rlast(), .S_AXI_MEM_rvalid(), .S_AXI_MEM_rready(1'b1),

            .S_AXI_CTRL_awid({IDW{1'b0}}), .S_AXI_CTRL_awaddr(sc_awaddr[g]),
            .S_AXI_CTRL_awlen(8'd0), .S_AXI_CTRL_awvalid(sc_awvalid[g]),
            .S_AXI_CTRL_awready(sc_awready[g]),
            .S_AXI_CTRL_wdata(sc_wdata[g]), .S_AXI_CTRL_wstrb(8'hFF),
            .S_AXI_CTRL_wlast(1'b1), .S_AXI_CTRL_wvalid(sc_wvalid[g]),
            .S_AXI_CTRL_wready(sc_wready[g]),
            .S_AXI_CTRL_bid(), .S_AXI_CTRL_bresp(),
            .S_AXI_CTRL_bvalid(sc_bvalid[g]), .S_AXI_CTRL_bready(1'b1),
            .S_AXI_CTRL_arid({IDW{1'b0}}), .S_AXI_CTRL_araddr(32'd0),
            .S_AXI_CTRL_arlen(8'd0), .S_AXI_CTRL_arvalid(1'b0),
            .S_AXI_CTRL_arready(),
            .S_AXI_CTRL_rid(), .S_AXI_CTRL_rdata(), .S_AXI_CTRL_rresp(),
            .S_AXI_CTRL_rlast(), .S_AXI_CTRL_rvalid(), .S_AXI_CTRL_rready(1'b1),

            .M_AXI_DRAM_awid(m_awid[g]), .M_AXI_DRAM_awaddr(m_awaddr[g]),
            .M_AXI_DRAM_awlen(m_awlen[g]), .M_AXI_DRAM_awsize(m_awsize[g]),
            .M_AXI_DRAM_awburst(m_awburst[g]),
            .M_AXI_DRAM_awvalid(m_awvalid[g]), .M_AXI_DRAM_awready(m_awready[g]),
            .M_AXI_DRAM_wdata(m_wdata[g]), .M_AXI_DRAM_wstrb(m_wstrb[g]),
            .M_AXI_DRAM_wlast(m_wlast[g]), .M_AXI_DRAM_wvalid(m_wvalid[g]),
            .M_AXI_DRAM_wready(m_wready[g]),
            .M_AXI_DRAM_bid(m_bid[g]), .M_AXI_DRAM_bresp(m_bresp[g]),
            .M_AXI_DRAM_bvalid(m_bvalid[g]), .M_AXI_DRAM_bready(m_bready[g]),
            .M_AXI_DRAM_arid(m_arid[g]), .M_AXI_DRAM_araddr(m_araddr[g]),
            .M_AXI_DRAM_arlen(m_arlen[g]), .M_AXI_DRAM_arsize(m_arsize[g]),
            .M_AXI_DRAM_arburst(m_arburst[g]),
            .M_AXI_DRAM_arvalid(m_arvalid[g]), .M_AXI_DRAM_arready(m_arready[g]),
            .M_AXI_DRAM_rid(m_rid[g]), .M_AXI_DRAM_rdata(m_rdata[g]),
            .M_AXI_DRAM_rresp(m_rresp[g]), .M_AXI_DRAM_rlast(m_rlast[g]),
            .M_AXI_DRAM_rvalid(m_rvalid[g]), .M_AXI_DRAM_rready(m_rready[g]),

            .M_AXIS_LINK0_tdata(o0_d[g]), .M_AXIS_LINK0_tuser(o0_u[g]),
            .M_AXIS_LINK0_tlast(o0_l[g]), .M_AXIS_LINK0_tvalid(o0_v[g]),
            .M_AXIS_LINK0_tready(1'b1),
            .S_AXIS_LINK0_tdata(HAS0 ? o1_d[g-1] : {LW{1'b0}}),
            .S_AXIS_LINK0_tuser(HAS0 ? o1_u[g-1] : {UW{1'b0}}),
            .S_AXIS_LINK0_tlast(HAS0 ? o1_l[g-1] : 1'b0),
            .S_AXIS_LINK0_tvalid(HAS0 ? o1_v[g-1] : 1'b0),
            .S_AXIS_LINK0_tready(),
            .M_AXIS_LINK1_tdata(o1_d[g]), .M_AXIS_LINK1_tuser(o1_u[g]),
            .M_AXIS_LINK1_tlast(o1_l[g]), .M_AXIS_LINK1_tvalid(o1_v[g]),
            .M_AXIS_LINK1_tready(1'b1),
            .S_AXIS_LINK1_tdata(HAS1 ? o0_d[g+1] : {LW{1'b0}}),
            .S_AXIS_LINK1_tuser(HAS1 ? o0_u[g+1] : {UW{1'b0}}),
            .S_AXIS_LINK1_tlast(HAS1 ? o0_l[g+1] : 1'b0),
            .S_AXIS_LINK1_tvalid(HAS1 ? o0_v[g+1] : 1'b0),
            .S_AXIS_LINK1_tready()
        );

        axi_ram #(.DATA_W(MW), .ADDR_W(AW), .ID_W(IDW), .WORDS(RAMW),
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

        // A hierarchical path to an instance that does not exist is an
        // elaboration error, so the readback is a wire array, not a task.
        assign bd_rd[g] = ram.mem[bd_a >> 1][(bd_a & 1) * 256 +: 256];
    end
    for (g = NMESH; g < 4; g = g + 1) begin : dead
        assign bd_rd[g] = {256{1'bx}};
    end
    endgenerate

    // ---- the source mover's AXI, for the burst monitor -------------------
    // Hierarchical because the generated top exposes neither status nor bus.
    wire        mv_busy   = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.stat_busy;
    wire [3:0]  mv_fault  = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.stat_fault;
    wire [31:0] mv_done   = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.stat_done;
    wire [AW-1:0] mv_araddr = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.m_araddr;
    wire [7:0]  mv_arlen   = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.m_arlen;
    wire        mv_arv     = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.m_arvalid;
    wire        mv_arr     = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.m_arready;
    wire [AW-1:0] mv_awaddr = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.m_awaddr;
    wire [7:0]  mv_awlen   = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.m_awlen;
    wire        mv_awv     = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.m_awvalid;
    wire        mv_awr     = mesh[SRCP].u.u_mag.u_mag.u_mag.u_mover.m_awready;

    integer merr = 0, n_ar = 0, n_aw = 0;
    reg [3:0] aw_mesh_seen;

    always @(posedge clk) if (resetn) begin
        if (mv_arv && mv_arr) begin
            n_ar = n_ar + 1;
            if (
                |mv_araddr[4:0]
                || (({1'b0, mv_araddr[11:5]} + {1'b0, mv_arlen} + 9'd1) > 9'd128)
            ) begin
                merr = merr + 1;
                $display("  ERROR AR %h len %0d", mv_araddr, mv_arlen);
            end
        end
        if (mv_awv && mv_awr) begin
            n_aw = n_aw + 1;
            aw_mesh_seen = mv_awaddr[AW-1 -: 4];
            if (
                |mv_awaddr[4:0]
                || (({1'b0, mv_awaddr[11:5]} + {1'b0, mv_awlen} + 9'd1) > 9'd128)
            ) begin
                merr = merr + 1;
                $display("  ERROR AW %h len %0d", mv_awaddr, mv_awlen);
            end
        end
    end

    // ---- DRAM readback and per-mesh write count --------------------------
    integer errors = 0, checks = 0, i, p, spin;
    integer t_busy, t_land, c0;
    reg [255:0] rd;

    // A transit MAG must never write its own DRAM, and counting the bus is a
    // stronger statement than comparing a marker it might have overwritten.
    integer nwr [0:3];

    // ITS OWN LOOP VARIABLE. Sharing `i` with the checking loop left `i` at 4
    // on every dclk edge, so every expected value was built from the wrong one.
    integer mi;
    always @(posedge dclk) if (resetn) begin
        for (mi = 0; mi < 4; mi = mi + 1) begin
            if (m_awvalid[mi] === 1'b1 && m_awready[mi] === 1'b1) begin
                nwr[mi] = nwr[mi] + 1;
            end
        end
    end

    task wget(input integer pp, input integer w, output [255:0] d);
        begin
            bd_a = w;
            #1;
            d = bd_rd[pp];
        end
    endtask

    // ---- the control window, exactly as software writes it ---------------
    localparam [31:0] A_MV_CFG = 32'h0800;

    task mvwr(input integer pp, input [7:0] a, input [63:0] d);
        begin
            @(negedge clk);
            sc_awaddr[pp] = A_MV_CFG + {24'd0, a}; sc_awvalid[pp] = 1'b1;
            spin = 0;
            while (spin < 3000) begin
                @(posedge clk);
                if (sc_awready[pp]) begin
                    spin = 9000;
                end
                else begin
                    spin = spin + 1;
                end
            end
            @(negedge clk); sc_awvalid[pp] = 1'b0;
            sc_wdata[pp] = d; sc_wvalid[pp] = 1'b1;
            spin = 0;
            while (spin < 3000) begin
                @(posedge clk);
                if (sc_wready[pp]) begin
                    spin = 9000;
                end
                else begin
                    spin = spin + 1;
                end
            end
            @(negedge clk); sc_wvalid[pp] = 1'b0;
            spin = 0;
            while (!sc_bvalid[pp] && spin < 3000) begin
                spin = spin + 1; @(negedge clk);
            end
        end
    endtask

    task mvhdr(input integer pp, input sel, input [AW-1:0] base, input [2:0] nd);
        begin mvwr(pp, 8'h10, {17'd0, nd, base, 3'd0, sel}); end
    endtask
    task mvdim(input integer pp, input sel, input [2:0] d, input [15:0] c,
               input signed [31:0] s);
        begin mvwr(pp, 8'h18, {12'd0, s, c, d, sel}); mvwr(pp, 8'h20, 64'd0); end
    endtask

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

    // Word 128 of each mesh, so nothing overlaps the agent's own scratch.
    localparam [AW-1:0] SRC_OFF = 40'h1000;
    localparam [AW-1:0] DST_OFF = 40'h2000;
    localparam [255:0] MARK = {8{32'hBEEF_0000}};

    real cpw, mbs;
    integer pj;

    // One measured move. `flg` is MV_CTRL.flags: 8'h20 caps a burst at one word,
    // which is the pre-burst read behaviour, and 8'h00 is the shipped default.
    task do_move(input [7:0] flg, input [255:0] tag);
        begin
            n_ar = 0; n_aw = 0; merr = 0;
            for (pj = 0; pj < 4; pj = pj + 1) begin
                nwr[pj] = 0;
            end

            mvhdr(SRCP, 1'b0, SRC_OFF, 3'd1);
            mvdim(SRCP, 1'b0, 3'd0, NW[15:0], 32'sd32);
            mvhdr(SRCP, 1'b1, DST_OFF | ({38'd0, dst_mesh} << 36), 3'd1);
            mvdim(SRCP, 1'b1, 3'd0, NW[15:0], 32'sd32);
            mvwr (SRCP, 8'h00, {47'd0, 1'b1, flg, 3'd0, 2'd1, 3'd0});

            spin = 0;
            while (!mv_busy && spin < 2000) begin
                spin = spin + 1; @(negedge clk);
            end
            c0 = cyc;
            spin = 0;
            while (mv_busy && spin < SPIN_MAX) begin
                spin = spin + 1; @(negedge clk);
            end
            t_busy = cyc - c0;
            chk(spin < SPIN_MAX, "mover went idle", 0);
            chk(mv_fault === 4'd0, "no fault", {28'd0, mv_fault});

            // A posted remote write clears stat_busy before the bytes land, so
            // the landing time is measured separately and the gap is the point.
            spin = 0;
            wget(DSTP, (DST_OFF >> 5) + NW - 1, rd);
            while ((rd !== {8{32'hAC00_0000 | (NW - 1)}}) && spin < SPIN_MAX) begin
                spin = spin + 1; @(negedge clk);
                wget(DSTP, (DST_OFF >> 5) + NW - 1, rd);
            end
            t_land = cyc - c0;
            chk(spin < SPIN_MAX, "last word landed", 0);

            for (i = 0; i < NW; i = i + 1) begin
                wget(DSTP, (DST_OFF >> 5) + i, rd);
                chk(rd === {8{32'hAC00_0000 | i[31:0]}}, "destination word", i);
            end

            // A MAG the packet only passes through must not write its own DRAM.
            for (p = 0; p < NMESH; p = p + 1) begin
                if ((p != SRCP) && (p != DSTP)) begin
                    chk(nwr[p] == 0, "transit MAG issued no DRAM write", nwr[p]);
                end
            end

            for (i = 0; i < NW; i = i + 1) begin
                wget(SRCP, (SRC_OFF >> 5) + i, rd);
                chk(rd === {8{32'hAC00_0000 | i[31:0]}}, "source unmodified", i);
            end

            if (NMESH > 1) begin
                chk(aw_mesh_seen === {2'b00, dst_mesh},
                    "AW carried the destination mesh", {28'd0, aw_mesh_seen});
            end

            errors = errors + merr;
            checks = checks + n_ar + n_aw;

            cpw = t_land * 1.0 / NW;
            mbs = (NW * 32.0 * 300.0) / t_land;
            // aw[39:36] is printed unconditionally: it is what separates "the
            // mover addressed the wrong mesh" from "the interlink lost it".
            $display("  %0d MAG %0s | busy %0d cyc | landed %0d cyc | %0d.%02d cyc/w | %0d MB/s at 300 MHz | %0d AR %0d AW | aw[39:36]=%b want %b",
                     NMESH, tag, t_busy, t_land, $rtoi(cpw),
                     $rtoi((cpw - $rtoi(cpw)) * 100.0), $rtoi(mbs), n_ar, n_aw,
                     aw_mesh_seen, {2'b00, dst_mesh});
            if (t_land > t_busy) begin
                $display("  stat_busy fell %0d cycles BEFORE the last byte landed",
                         t_land - t_busy);
            end
        end
    endtask

`ifdef MV_L2
    // THE MOVER REACHING L2 BY ADDRESS: DRAM -> aperture 0 -> DRAM, the only
    // path that exercises the mover as a requester of mag_stage_port.
    localparam [AW-1:0] A_STG = 40'h80_0000_0000 | (cat(SRCP) << 36);
    localparam [AW-1:0] BACK_OFF = 40'h3000;
    integer li;
    task l2_round_trip;
        begin
            for (li = 0; li < NW; li = li + 1) begin
                mesh[SRCP].ram.mem[((BACK_OFF >> 5) + li) >> 1]
                                  [((((BACK_OFF >> 5) + li) & 1)) * 256 +: 256]
                    = {8{32'hDEAD_DEAD}};
            end

            // DRAM -> L2
            mvhdr(SRCP, 1'b0, SRC_OFF, 3'd1);
            mvdim(SRCP, 1'b0, 3'd0, NW[15:0], 32'sd32);
            mvhdr(SRCP, 1'b1, A_STG, 3'd1);
            mvdim(SRCP, 1'b1, 3'd0, NW[15:0], 32'sd32);
            mvwr (SRCP, 8'h00, {47'd0, 1'b1, 8'h00, 3'd0, 2'd1, 3'd0});
            spin = 0;
            while (!mv_busy && spin < 2000) begin spin = spin + 1; @(negedge clk); end
            spin = 0;
            while (mv_busy && spin < SPIN_MAX) begin spin = spin + 1; @(negedge clk); end
            chk(spin < SPIN_MAX, "L2 write: mover went idle", 0);
            chk(mv_fault === 4'd0, "L2 write: no fault", {28'd0, mv_fault});

            // L2 -> DRAM, somewhere the first move never touched
            mvhdr(SRCP, 1'b0, A_STG, 3'd1);
            mvdim(SRCP, 1'b0, 3'd0, NW[15:0], 32'sd32);
            mvhdr(SRCP, 1'b1, BACK_OFF, 3'd1);
            mvdim(SRCP, 1'b1, 3'd0, NW[15:0], 32'sd32);
            mvwr (SRCP, 8'h00, {47'd0, 1'b1, 8'h00, 3'd0, 2'd1, 3'd0});
            spin = 0;
            while (!mv_busy && spin < 2000) begin spin = spin + 1; @(negedge clk); end
            spin = 0;
            while (mv_busy && spin < SPIN_MAX) begin spin = spin + 1; @(negedge clk); end
            chk(spin < SPIN_MAX, "L2 read: mover went idle", 0);
            chk(mv_fault === 4'd0, "L2 read: no fault", {28'd0, mv_fault});

            // DEAD_DEAD surviving means the store never answered.
            spin = 0;
            wget(SRCP, (BACK_OFF >> 5) + NW - 1, rd);
            while ((rd !== {8{32'hAC00_0000 | (NW - 1)}}) && spin < SPIN_MAX) begin
                spin = spin + 1; @(negedge clk);
                wget(SRCP, (BACK_OFF >> 5) + NW - 1, rd);
            end
            for (li = 0; li < NW; li = li + 1) begin
                wget(SRCP, (BACK_OFF >> 5) + li, rd);
                chk(rd === {8{32'hAC00_0000 | li[31:0]}}, "L2 round trip word", li);
            end
            errors = errors + merr;
            $display("  L2 round trip: DRAM -> aperture 0 -> DRAM, %0d words", NW);
        end
    endtask
`endif

    initial begin
        sc_awvalid = 0; sc_wvalid = 0;
        for (i = 0; i < 4; i = i + 1) begin
            sc_awaddr[i] = 0; sc_wdata[i] = 0;
            nwr[i] = 0;
        end
        aw_mesh_seen = 4'hF;
        bd_a = 0;

        // The source is always chain position 0, so this path always exists.
        for (i = 0; i < NW; i = i + 1) begin
            mesh[0].ram.mem[((SRC_OFF >> 5) + i) >> 1]
                           [((((SRC_OFF >> 5) + i) & 1)) * 256 +: 256]
                = {8{32'hAC00_0000 | i[31:0]}};
        end

        repeat (20) @(negedge clk);
        resetn = 1'b1;
        repeat (40) @(negedge clk);

        $display("--- %0d MAG(s): mesh%0d -> mesh%0d, %0d hops, %0d words ---",
                 NMESH, cat(SRCP), cat(DSTP),
                 (DSTP > SRCP) ? (DSTP - SRCP) : (SRCP - DSTP), NW);

        do_move(8'h20, "cap1  ");
        do_move(8'h00, "rdburst");
        // flags[3] coalesces writes. Safe only once mag_ilink's splitter streams
        // a burst rather than taking one AW and one W beat.
        do_move(8'h08, "rd+wr ");

`ifdef MV_L2
        l2_round_trip;
`endif

        if (errors == 0) begin
            $display("  PASS mover_chain %0d MAG: %0d checks", NMESH, checks);
        end
        else begin
            $display("  FAIL mover_chain %0d MAG: %0d errors, %0d checks",
                     NMESH, errors, checks);
        end
        $finish;
    end

    initial begin
        #3000000;
        $display("  FAIL mover_chain: watchdog");
        $finish;
    end
endmodule

// One MAG, no interlink: the control that isolates bursting from link cost.
module mover_chain1_tb;
    mover_chain_core #(.NMESH(1), .SRCP(0), .DSTP(0), .NW(64)) c ();
endmodule

// Two MAGs, one hop: mesh0 reads its own DRAM, mesh1's DRAM receives.
module mover_chain2_tb;
    mover_chain_core #(.NMESH(2), .SRCP(0), .DSTP(1), .NW(64)) c ();
endmodule

// Four MAGs, THREE hops: mesh0 -> mesh2 transits mesh1 and mesh3.
module mover_chain4_tb;
    mover_chain_core #(.NMESH(4), .SRCP(0), .DSTP(3), .NW(64)) c ();
endmodule

`default_nettype wire
