// mm_mesh_tb's mover path through mag_dram_port instead of NCH masters. RAM is
// 512 wide: logical word `w` is mem[w>>1] half w[0] -- use wput/wget, always.

`default_nettype none
`timescale 1ns/1ps

module mm_mesh_1m_tb;
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4;
    localparam MEMP = 1, NCH = MEMP + 2, MW = 512;

    localparam [3:0] T_CU_INST = 4'h5;
    localparam CX = 1, CY = 1;

    reg clk = 0, rst = 1, dclk = 0;
    always begin
        #2   clk  = ~clk;
    end
    always begin
        #1.7 dclk = ~dclk;
    end

    reg  [FW-1:0] ext_i;
    reg           ext_iv;
    wire          ext_ib;
    wire [FW-1:0] ext_o;
    wire          ext_ov;

    reg  [31:0]  sc_awaddr = 0;
    reg          sc_awvalid = 0;
    reg  [63:0]  sc_wdata = 0;
    reg          sc_wvalid = 0;
    wire         sc_awready, sc_wready, sc_bvalid;
    wire         mv_busy;
    wire [3:0]   mv_fault;
    wire [31:0]  mv_done;

    wire [IDW-1:0]  m_awid, m_arid, m_bid, m_rid;
    wire [AW-1:0]   m_awaddr, m_araddr;
    wire [7:0]      m_awlen, m_arlen;
    wire [2:0]      m_awsize, m_arsize;
    wire [1:0]      m_awburst, m_arburst, m_bresp, m_rresp;
    wire            m_awvalid, m_awready, m_arvalid, m_arready;
    wire [MW-1:0]   m_wdata, m_rdata;
    wire [MW/8-1:0] m_wstrb;
    wire            m_wlast, m_wvalid, m_wready;
    wire            m_bvalid, m_bready, m_rlast, m_rvalid, m_rready;

    wire [47:0] dbg_cluster;
    wire [31:0] dbg_vcyc, obs;
    wire        dbg_vflt;

    mm_mesh_1m #(.FW(FW), .PW(PW), .DW(DW), .AW(AW), .IDW(IDW), .MEMP(MEMP),
                 .MW(MW), .MODEL(1)) dut (
        .clk(clk), .rst(rst),
        .dram_aclk(dclk), .dram_aresetn(!rst),
        .sm_awaddr({AW{1'b0}}), .sm_awlen(8'd0), .sm_awvalid(1'b0),
        .sm_wdata({DW{1'b0}}), .sm_wlast(1'b0), .sm_wvalid(1'b0),
        .sc_awaddr(sc_awaddr), .sc_awvalid(sc_awvalid), .sc_awready(sc_awready),
        .sc_wdata(sc_wdata), .sc_wvalid(sc_wvalid), .sc_wready(sc_wready),
        .sc_bvalid(sc_bvalid),
        .sc_araddr(32'd0), .sc_arvalid(1'b0), .sc_rdata(), .sc_rvalid(),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen),
        .m_awsize(m_awsize), .m_awburst(m_awburst),
        .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid),
        .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen),
        .m_arsize(m_arsize), .m_arburst(m_arburst),
        .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp),
        .m_rlast(m_rlast), .m_rvalid(m_rvalid), .m_rready(m_rready),
        .ext_in_data(ext_i), .ext_in_valid(ext_iv), .ext_in_busy(ext_ib),
        .ext_out_data(ext_o), .ext_out_valid(ext_ov), .ext_out_busy(1'b0),
        .dbg_cluster(dbg_cluster), .dbg_vec_cycles(dbg_vcyc),
        .dbg_vec_fault(dbg_vflt), .obs(obs)
    );

    axi_ram #(.DATA_W(MW), .ADDR_W(AW), .ID_W(IDW), .WORDS(2048),
              .PORTS(1)) u_ram (
        .clk(dclk), .resetn(!rst),
        .s_awid(m_awid), .s_awaddr(m_awaddr), .s_awlen(m_awlen),
        .s_awsize(m_awsize), .s_awburst(m_awburst),
        .s_awvalid(m_awvalid), .s_awready(m_awready),
        .s_wdata(m_wdata), .s_wstrb(m_wstrb), .s_wlast(m_wlast),
        .s_wvalid(m_wvalid), .s_wready(m_wready),
        .s_bid(m_bid), .s_bresp(m_bresp), .s_bvalid(m_bvalid),
        .s_bready(m_bready),
        .s_arid(m_arid), .s_araddr(m_araddr), .s_arlen(m_arlen),
        .s_arsize(m_arsize), .s_arburst(m_arburst),
        .s_arvalid(m_arvalid), .s_arready(m_arready),
        .s_rid(m_rid), .s_rdata(m_rdata), .s_rresp(m_rresp),
        .s_rlast(m_rlast), .s_rvalid(m_rvalid), .s_rready(m_rready),
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({MW{1'b0}}), .bd_rdata()
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

    // The 512-bit RAM, addressed in logical 256-bit words.
    task wput(input integer w, input [255:0] d);
        begin u_ram.mem[w >> 1][(w & 1) * 256 +: 256] = d; end
    endtask
    function [255:0] wget(input integer w);
        begin wget = u_ram.mem[w >> 1][(w & 1) * 256 +: 256]; end
    endfunction

    task send_inst(input [3:0] dx, input [3:0] dy, input [255:0] payload);
        begin
            @(negedge clk);
            while (ext_ib) begin
                @(negedge clk);
            end
            ext_i  <= {dx, dy, CX[3:0], CY[3:0], T_CU_INST, 8'h40, 1'b0, 3'b000,
                       payload};
            ext_iv <= 1'b1;
            @(negedge clk);
            ext_iv <= 1'b0;
        end
    endtask

    localparam [31:0] A_MV_CFG = 32'h0800;
    task mvwr(input [7:0] a, input [63:0] d);
        begin
            @(negedge clk);
            sc_awaddr = A_MV_CFG + {24'd0, a}; sc_awvalid = 1'b1;
            while (!sc_awready) begin
                @(negedge clk);
            end
            @(negedge clk);
            sc_awvalid = 1'b0;
            sc_wdata = d; sc_wvalid = 1'b1;
            while (!sc_wready) begin
                @(negedge clk);
            end
            @(negedge clk);
            sc_wvalid = 1'b0;
            while (!sc_bvalid) begin
                @(negedge clk);
            end
            @(negedge clk);
        end
    endtask
    task mvhdr(input sel, input [33:0] base, input [2:0] nd);
        begin mvwr(8'h10, {17'd0, nd, 6'd0, base, 3'd0, sel}); end
    endtask
    task mvdim(input sel, input [2:0] d, input [15:0] c,
               input signed [31:0] s);
        begin
            mvwr(8'h18, {12'd0, s, c, d, sel});
            mvwr(8'h20, 64'd0);
        end
    endtask

    task vec_inst(input [255:0] p); begin send_inst(4'd1, 4'd0, p); end endtask
    task cl_inst (input [255:0] p); begin send_inst(4'd2, 4'd1, p); end endtask
    task put_imem(input [8:0] a, input [31:0] w);
        begin vec_inst({4'd1, a, 211'd0, w}); end
    endtask
    task put_desc(input [2:0] ad, input [2:0] f, input [33:0] v);
        begin vec_inst({4'd2, ad, f, v, 212'd0}); end
    endtask

    localparam [31:0] I_VSETI  = 32'hD0000000;
    localparam [31:0] I_VSETVL = 32'hC0000000;
    localparam [31:0] I_VSETMD = 32'hC8000000;
    localparam [31:0] I_VFILL  = 32'hE8000000;
    localparam [31:0] I_VBAR   = 32'hE0000000;
    localparam [31:0] I_VLD    = 32'hA1200000;
    localparam [31:0] I_VADD   = 32'h18020000;
    localparam [31:0] I_VST    = 32'hA9620000;
    localparam [31:0] I_VDRAIN = 32'hF0400001;
    localparam [31:0] I_VHALT  = 32'hF8000000;

    integer i, sig_count;

    always @(posedge clk) begin
        if (rst) begin
            sig_count <= 0;
        end
        else if (ext_ov && ext_o[FW-4*PW-1 -: 4] == 4'h6) begin
            sig_count <= sig_count + 1;
        end
    end

    localparam [33:0] A_MSRC = 34'h1000;   // logical word 128
    localparam [33:0] A_MDST = 34'h2000;   // logical word 256
    localparam [33:0] A_DST  = 34'h8000;   // word 1024
    localparam [33:0] A_VSRC = 34'h9000;   // word 1152

    initial begin
        ext_i = 0; ext_iv = 0;
        for (i = 0; i < 2048; i = i + 1) begin
            u_ram.mem[i] = {MW{1'b0}};
        end
        for (i = 0; i < 16; i = i + 1) begin
            wput((A_MSRC >> 5) + i, {8{32'h7E00_0000 | i[31:0]}});
        end
        wput(A_VSRC >> 5, {16{16'h4000}});   // 2.0 in sixteen FP16 lanes

        repeat (10) @(negedge clk);
        rst = 0;
        repeat (20) @(negedge clk);

        // The mover's 4x4 word transpose, now through one packed master.
        $display("--- mover: 4x4 word transpose through mag_dram_port ---");
        mvhdr(1'b0, A_MSRC, 3'd2);
        mvdim(1'b0, 3'd0, 16'd4, 32'sd128);
        mvdim(1'b0, 3'd1, 16'd4, 32'sd32);
        mvhdr(1'b1, A_MDST, 3'd2);
        mvdim(1'b1, 3'd0, 16'd4, 32'sd32);
        mvdim(1'b1, 3'd1, 16'd4, 32'sd128);
        mvwr(8'h00, {47'd0, 1'b1, 8'd0, 3'd0, 2'd1, 3'd0});
        @(negedge clk);
        spin = 0;
        while (mv_busy && spin < 200000) begin spin = spin + 1; @(negedge clk); end
        chk(spin < 200000, "mover went idle", 0);
        chk(mv_fault === 4'd0, "mover did not fault", 0);
        repeat (200) @(negedge clk);
        for (i = 0; i < 16; i = i + 1) begin
            chk(wget((A_MDST >> 5) + i) === {8{32'h7E00_0000 | ((i%4)*4 + (i/4))}},
                "transposed word", i);
        end

        // The vector core: VFILL reads DRAM through MAG, VDRAIN writes it back,
        // so reads and writes are outstanding together on the one master.
        $display("--- vector core: DRAM -> lanes -> DRAM, over the NoC ---");
        put_imem(9'd0,  I_VSETI);   put_imem(9'd1, 32'd16);
        put_imem(9'd2,  I_VSETVL);  put_imem(9'd3, I_VSETMD);
        put_imem(9'd4,  I_VFILL);   put_imem(9'd5, I_VBAR);
        put_imem(9'd6,  I_VLD);     put_imem(9'd7, I_VADD);
        put_imem(9'd8,  I_VST);     put_imem(9'd9, I_VDRAIN);
        put_imem(9'd10, I_VHALT);
        put_desc(3'd0, 3'd0, A_VSRC);
        put_desc(3'd0, 3'd1, {18'd32, 16'd1});
        put_desc(3'd1, 3'd0, 34'd0);
        put_desc(3'd1, 3'd1, {18'd1, 16'd1});
        put_desc(3'd2, 3'd0, A_DST);
        put_desc(3'd2, 3'd1, {18'd32, 16'd1});
        put_desc(3'd3, 3'd0, 34'd1);
        put_desc(3'd3, 3'd1, {18'd1, 16'd1});

        vec_inst({4'd3, 9'd0, 243'd0});
        spin = 0;
        while ((sig_count < 20) && (spin < 400000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(spin < 400000, "vector kernel retired", 0);
        chk(dbg_vflt === 1'b0, "vector core did not fault", 0);
        repeat (200) @(negedge clk);
        chk(wget(A_DST >> 5) === {16{16'h4400}}, "vector result reached DRAM", 0);

        // The cluster's FILL, the third distinct path through the port.
        $display("--- cluster: a FILL served by MAG over the mesh ---");
        cl_inst({4'd1, A_MSRC, 16'd1, 1'b0, 1'b0, 8'd0, 8'd0, 8'd0, 8'd0,
                 24'd0, 2'd0, 1'b0, 8'd0, 8'd0, 8'd0, 1'b0, 1'b0, 115'd0});
        spin = 0;
        while ((dbg_cluster[47:32] == 16'd0) && (spin < 200000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(spin < 200000, "cluster FILL completed", 0);

        if (errors == 0) begin
            $display("PASS mm_mesh_1m_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL mm_mesh_1m_tb: %0d errors, %0d checks",
                     errors, checks);
        end
        $finish;
    end

    initial begin
        #2000000;
        $display("FAIL mm_mesh_1m_tb: watchdog (mv_busy=%0b fault=%0d)",
                 mv_busy, mv_fault);
        $finish;
    end
endmodule

`default_nettype wire
