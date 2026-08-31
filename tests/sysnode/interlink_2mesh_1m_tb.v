// TWO WHOLE MESHES over the interlink, each behind one packed AXI master.
// interlink_2mesh_tb drives mag_ilink and mag_switch alone; this carries MAGs.

`timescale 1ns / 1ps
`default_nettype none

module interlink_2mesh_1m_tb;
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4, MW = 512;
    localparam LW = 288, UW = 96;

    reg clk = 0, resetn = 0, dclk = 0;
    always begin
        #2   clk  = ~clk;
    end
    always begin
        #1.7 dclk = ~dclk;
    end

    // On the SLR chain mesh0 is the bottom end, so it reaches mesh1 by its UP
    // link: link1 of mesh0 faces link0 of mesh1, and the other two are dead.
    wire [LW-1:0] o0_f [0:1], o1_f [0:1];
    wire [1:0]    o0_v, o0_vc, o0_l, o1_v, o1_vc, o1_l;
    wire [3:0]    o0_cn [0:1], o1_cn [0:1];
    wire [1:0]    o0_cv, o0_cvc, o1_cv, o1_cvc;

    // Per-mesh control slave and one packed master each.
    reg  [31:0] sc_awaddr [0:1];
    reg  [1:0]  sc_awvalid, sc_wvalid;
    reg  [63:0] sc_wdata  [0:1];
    wire [1:0]  sc_awready, sc_wready, sc_bvalid;

    wire [IDW-1:0]  m_awid  [0:1], m_arid [0:1], m_bid [0:1], m_rid [0:1];
    wire [AW-1:0]   m_awaddr[0:1], m_araddr[0:1];
    wire [7:0]      m_awlen [0:1], m_arlen [0:1];
    wire [2:0]      m_awsize[0:1], m_arsize[0:1];
    wire [1:0]      m_awburst[0:1], m_arburst[0:1], m_bresp[0:1], m_rresp[0:1];
    wire [1:0]      m_awvalid, m_awready, m_arvalid, m_arready;
    wire [MW-1:0]   m_wdata [0:1], m_rdata[0:1];
    wire [MW/8-1:0] m_wstrb [0:1];
    wire [1:0]      m_wlast, m_wvalid, m_wready;
    wire [1:0]      m_bvalid, m_bready, m_rlast, m_rvalid, m_rready;

    // MAG_CDC's generate branch is only elaborated when it is taken, so without
    // this define the noc_local_cdc wrapping MAG's ports has no cover at all.
`ifdef MM_MAG_CDC
    localparam MAGCDC = 1;
`else
    localparam MAGCDC = 0;
`endif

    genvar g;
    generate for (g = 0; g < 2; g = g + 1) begin : mesh
        ktpu_min_1m #(.MESH_ID(g), .MODEL(1), .MW(MW),
                      .MAG_CDC(MAGCDC)) u (
            .axi_aclk(clk), .axi_aresetn(resetn),
            .hs_addr(32'd0), .hs_wr(1'b0), .hs_wdata(64'd0), .hs_wstrb(8'd0),
            .hs_rd(1'b0),
            .noc_clk(clk),
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

            .LINK0_OUT_valid(o0_v[g]), .LINK0_OUT_vc(o0_vc[g]),
            .LINK0_OUT_last(o0_l[g]), .LINK0_OUT_flit(o0_f[g]),
            .LINK0_OUT_crd_valid(g == 1 ? o1_cv[0] : 1'b0),
            .LINK0_OUT_crd_vc(g == 1 ? o1_cvc[0] : 1'b0),
            .LINK0_OUT_crd_n(g == 1 ? o1_cn[0] : 4'd0),
            .LINK0_IN_valid(g == 1 ? o1_v[0] : 1'b0),
            .LINK0_IN_vc(g == 1 ? o1_vc[0] : 1'b0),
            .LINK0_IN_last(g == 1 ? o1_l[0] : 1'b0),
            .LINK0_IN_flit(g == 1 ? o1_f[0] : {LW{1'b0}}),
            .LINK0_IN_crd_valid(o0_cv[g]), .LINK0_IN_crd_vc(o0_cvc[g]),
            .LINK0_IN_crd_n(o0_cn[g]),
            .LINK1_OUT_valid(o1_v[g]), .LINK1_OUT_vc(o1_vc[g]),
            .LINK1_OUT_last(o1_l[g]), .LINK1_OUT_flit(o1_f[g]),
            .LINK1_OUT_crd_valid(g == 0 ? o0_cv[1] : 1'b0),
            .LINK1_OUT_crd_vc(g == 0 ? o0_cvc[1] : 1'b0),
            .LINK1_OUT_crd_n(g == 0 ? o0_cn[1] : 4'd0),
            .LINK1_IN_valid(g == 0 ? o0_v[1] : 1'b0),
            .LINK1_IN_vc(g == 0 ? o0_vc[1] : 1'b0),
            .LINK1_IN_last(g == 0 ? o0_l[1] : 1'b0),
            .LINK1_IN_flit(g == 0 ? o0_f[1] : {LW{1'b0}}),
            .LINK1_IN_crd_valid(o1_cv[g]), .LINK1_IN_crd_vc(o1_cvc[g]),
            .LINK1_IN_crd_n(o1_cn[g])
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

    integer errors = 0, checks = 0, i, spin;

    task wput(input integer m, input integer w, input [255:0] d);
        begin
            if (m == 0) begin
                mesh[0].ram.mem[w >> 1][(w & 1) * 256 +: 256] = d;
            end
            else begin
                mesh[1].ram.mem[w >> 1][(w & 1) * 256 +: 256] = d;
            end
        end
    endtask
    function [255:0] wget(input integer m, input integer w);
        begin
            wget = (m == 0) ? mesh[0].ram.mem[w >> 1][(w & 1) * 256 +: 256]
                            : mesh[1].ram.mem[w >> 1][(w & 1) * 256 +: 256];
        end
    endfunction

    localparam [31:0] A_MV_CFG = 32'h0800;
    task mvwr(input integer m, input [7:0] a, input [63:0] d);
        begin
            @(negedge clk);
            sc_awaddr[m] = A_MV_CFG + {24'd0, a}; sc_awvalid[m] = 1'b1;
            spin = 0;
            while (spin < 3000) begin
                @(posedge clk);
                if (sc_awready[m]) begin
                    spin = 9000;
                end
                else begin
                    spin = spin + 1;
                end
            end
            @(negedge clk); sc_awvalid[m] = 1'b0;
            sc_wdata[m] = d; sc_wvalid[m] = 1'b1;
            spin = 0;
            while (spin < 3000) begin
                @(posedge clk);
                if (sc_wready[m]) begin
                    spin = 9000;
                end
                else begin
                    spin = spin + 1;
                end
            end
            @(negedge clk); sc_wvalid[m] = 1'b0;
            spin = 0;
            while (!sc_bvalid[m] && spin < 3000) begin
                spin = spin + 1; @(negedge clk);
            end
        end
    endtask
    task mvhdr(input integer m, input sel, input [AW-1:0] base, input [2:0] nd);
        begin mvwr(m, 8'h10, {17'd0, nd, base, 3'd0, sel}); end
    endtask
    task mvdim(input integer m, input sel, input [2:0] d, input [15:0] c,
               input signed [31:0] s);
        begin mvwr(m, 8'h18, {12'd0, s, c, d, sel}); mvwr(m, 8'h20, 64'd0); end
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

    localparam [39:0] SRC = 40'h1000;                              // mesh0 word 128
    // Mesh is [37:36] now. At [33:32] this addressed mesh 0's own DRAM and the
    // remote write landed locally -- wrong data, no fault.
    localparam [39:0] DST_REMOTE = {2'b00, 2'b01, 4'h0, 32'h2000}; // mesh1 word 256

    initial begin
        sc_awvalid = 0; sc_wvalid = 0;
        sc_awaddr[0] = 0; sc_awaddr[1] = 0;
        sc_wdata[0]  = 0; sc_wdata[1]  = 0;
        for (i = 0; i < 2048; i = i + 1) begin
            mesh[0].ram.mem[i] = {MW{1'b0}};
            mesh[1].ram.mem[i] = {MW{1'b0}};
        end
        for (i = 0; i < 8; i = i + 1) begin
            wput(0, (SRC >> 5) + i, {8{32'hAC00_0000 | i[31:0]}});
        end

        repeat (20) @(negedge clk);
        resetn = 1'b1;
        repeat (40) @(negedge clk);

        // DRAM -> DRAM ACROSS THE LINK: mesh0's mover reads its own memory and
        // writes an address whose [37:36] names mesh 1.
        $display("--- DRAM(mesh0) -> link -> DRAM(mesh1) ---");
        mvhdr(0, 1'b0, SRC, 3'd1);
        mvdim(0, 1'b0, 3'd0, 16'd8, 32'sd32);
        mvhdr(0, 1'b1, DST_REMOTE, 3'd1);
        mvdim(0, 1'b1, 3'd0, 16'd8, 32'sd32);
        mvwr (0, 8'h00, {47'd0, 1'b1, 8'd0, 3'd0, 2'd1, 3'd0});

        // The generated top exposes no mover status, so wait on the far DRAM.
        spin = 0;
        while ((wget(1, (32'h2000 >> 5)) === {MW/2{1'b0}}) && spin < 300000) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(spin < 300000, "first remote word arrived", 0);
        repeat (5000) @(negedge clk);

        for (i = 0; i < 8; i = i + 1) begin
            chk(wget(1, (32'h2000 >> 5) + i) === {8{32'hAC00_0000 | i[31:0]}},
                "remote word landed in mesh1 DRAM", i);
        end

        // Indented, because xsim.py's verdict is `"  PASS" in out` and its
        // output filter keeps only indented lines. Flush left, it never reports.
        if (errors == 0) begin
            $display("  PASS interlink_2mesh_1m_tb: %0d checks", checks);
        end
        else begin
            $display("  FAIL interlink_2mesh_1m_tb: %0d errors, %0d checks",
                     errors, checks);
        end
        $finish;
    end

    initial begin
        #4000000;
        $display("  FAIL interlink_2mesh_1m_tb: watchdog");
        $finish;
    end
endmodule

`default_nettype wire
