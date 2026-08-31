// The UPLOAD path: host AXI slave -> MAG -> one packed master -> DRAM. Weights
// cannot load without it, and mm_mesh leaves sm_awready/sm_wready unconnected.

`default_nettype none
`timescale 1ns/1ps

module mag_1m_upload_tb;
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4, MW = 512;
    localparam MEMP = 2;

    reg clk = 0, resetn = 0, dclk = 0;
    always begin
        #2   clk  = ~clk;
    end
    always begin
        #1.7 dclk = ~dclk;
    end

    reg  [AW-1:0]   sm_awaddr = 0;
    reg  [7:0]      sm_awlen  = 0;
    reg             sm_awvalid = 0;
    wire            sm_awready;
    reg  [DW-1:0]   sm_wdata = 0;
    reg             sm_wlast = 0, sm_wvalid = 0;
    wire            sm_wready, sm_bvalid;

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

    sysnode #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW),
             .ID_W(IDW), .PORTS(MEMP), .MW(MW)) dut (
        .clk(clk), .resetn(resetn),
        .dram_aclk(dclk), .dram_aresetn(resetn),
        .sm_awid({IDW{1'b0}}), .sm_awaddr(sm_awaddr), .sm_awlen(sm_awlen),
        .sm_awvalid(sm_awvalid), .sm_awready(sm_awready),
        .sm_wdata(sm_wdata), .sm_wstrb({(DW/8){1'b1}}), .sm_wlast(sm_wlast),
        .sm_wvalid(sm_wvalid), .sm_wready(sm_wready),
        .sm_bid(), .sm_bresp(), .sm_bvalid(sm_bvalid), .sm_bready(1'b1),
        .sm_arid({IDW{1'b0}}), .sm_araddr({AW{1'b0}}), .sm_arlen(8'd0),
        .sm_arvalid(1'b0), .sm_arready(),
        .sm_rid(), .sm_rdata(), .sm_rresp(), .sm_rlast(), .sm_rvalid(),
        .sm_rready(1'b1),
        .sc_awid({IDW{1'b0}}), .sc_awaddr(32'd0), .sc_awlen(8'd0),
        .sc_awvalid(1'b0), .sc_awready(),
        .sc_wdata(64'd0), .sc_wstrb(8'hFF), .sc_wlast(1'b0), .sc_wvalid(1'b0),
        .sc_wready(),
        .sc_bid(), .sc_bresp(), .sc_bvalid(), .sc_bready(1'b1),
        .sc_arid({IDW{1'b0}}), .sc_araddr(32'd0), .sc_arlen(8'd0),
        .sc_arvalid(1'b0), .sc_arready(),
        .sc_rid(), .sc_rdata(), .sc_rresp(), .sc_rlast(), .sc_rvalid(),
        .sc_rready(1'b1),
        .mem_in_data({(MEMP*FW){1'b0}}), .mem_in_valid({MEMP{1'b0}}),
        .mem_in_busy(),
        .mem_out_data(), .mem_out_valid(), .mem_out_busy({MEMP{1'b0}}),
        .mem_rd_count(), .mem_wr_count(),
        .mv_busy(), .mv_fault(), .mv_done(),
        .pe_halt_req(1'b0), .pe_status(), .pe_busy(),
        .dram_awid(m_awid), .dram_awaddr(m_awaddr), .dram_awlen(m_awlen),
        .dram_awsize(m_awsize), .dram_awburst(m_awburst),
        .dram_awvalid(m_awvalid), .dram_awready(m_awready),
        .dram_wdata(m_wdata), .dram_wstrb(m_wstrb), .dram_wlast(m_wlast),
        .dram_wvalid(m_wvalid), .dram_wready(m_wready),
        .dram_bid(m_bid), .dram_bresp(m_bresp), .dram_bvalid(m_bvalid),
        .dram_bready(m_bready),
        .dram_arid(m_arid), .dram_araddr(m_araddr), .dram_arlen(m_arlen),
        .dram_arsize(m_arsize), .dram_arburst(m_arburst),
        .dram_arvalid(m_arvalid), .dram_arready(m_arready),
        .dram_rid(m_rid), .dram_rdata(m_rdata), .dram_rresp(m_rresp),
        .dram_rlast(m_rlast), .dram_rvalid(m_rvalid), .dram_rready(m_rready),
        .link0_out_valid(), .link0_out_vc(), .link0_out_last(),
        .link0_out_flit(), .link0_out_crd_valid(1'b0), .link0_out_crd_vc(1'b0),
        .link0_out_crd_n(4'd0),
        .link0_in_valid(1'b0), .link0_in_vc(1'b0), .link0_in_last(1'b0),
        .link0_in_flit(288'd0), .link0_in_crd_valid(), .link0_in_crd_vc(),
        .link0_in_crd_n(),
        .link1_out_valid(), .link1_out_vc(), .link1_out_last(),
        .link1_out_flit(), .link1_out_crd_valid(1'b0), .link1_out_crd_vc(1'b0),
        .link1_out_crd_n(4'd0),
        .link1_in_valid(1'b0), .link1_in_vc(1'b0), .link1_in_last(1'b0),
        .link1_in_flit(288'd0), .link1_in_crd_valid(), .link1_in_crd_vc(),
        .link1_in_crd_n()
    );

    axi_ram #(.DATA_W(MW), .ADDR_W(AW), .ID_W(IDW), .WORDS(2048),
              .PORTS(1)) u_ram (
        .clk(dclk), .resetn(resetn),
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

    integer errors = 0, checks = 0, i, spin;
    integer aw_seen = 0, w_seen = 0;

    always @(posedge dclk) begin
        if (m_awvalid && m_awready) begin
            aw_seen = aw_seen + 1;
        end
        if (m_wvalid  && m_wready) begin
            w_seen  = w_seen  + 1;
        end
    end
    reg [DW-1:0] golden [0:63];

    function [255:0] wget(input integer w);
        begin wget = u_ram.mem[w >> 1][(w & 1) * 256 +: 256]; end
    endfunction

    // One host burst of `n` words starting at logical word `w0`.
    task upload(input integer w0, input integer n);
        integer k;
        begin
            @(negedge clk);
            sm_awaddr  = w0 * (DW / 8);
            sm_awlen   = n - 1;
            // AXI: the beat moves on the posedge where valid AND ready are
            // both high, so ready must be sampled BEFORE that edge, not after.
            sm_awvalid = 1'b1;
            spin = 0;
            while (spin < 2000) begin
                @(posedge clk);
                if (sm_awready) begin
                    spin = 3000;
                end
                else begin
                    spin = spin + 1;
                end
            end
            if (spin == 2000) begin
                $display("  STUCK: sm_awready never asserted");
            end
            @(negedge clk);
            sm_awvalid = 1'b0;
            for (k = 0; k < n; k = k + 1) begin
                golden[k] = {8{32'hC0DE_0000 | (w0 + k)}};
                sm_wdata  = golden[k];
                sm_wlast  = (k == n - 1);
                sm_wvalid = 1'b1;
                spin = 0;
                while (spin < 2000) begin
                    @(posedge clk);
                    if (sm_wready) begin
                        spin = 3000;
                    end
                    else begin
                        spin = spin + 1;
                    end
                end
                if (spin == 2000) begin
                    $display("  STUCK: sm_wready never asserted, beat %0d", k);
                end
                @(negedge clk);
            end
            sm_wvalid = 1'b0; sm_wlast = 1'b0;
        end
    endtask

    task chk(input cond, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 10) begin
                    $display("  FAIL upload word %0d", where);
                end
            end
        end
    endtask

    initial begin
        repeat (20) @(negedge clk);
        resetn = 1'b1;
        repeat (20) @(negedge clk);

        // Even start, even length; then odd start and odd length, so the
        // packer's head and tail phases both run on the upload path too.
        upload(64, 8);
        repeat (400) @(negedge clk);
        for (i = 0; i < 8; i = i + 1) begin
            chk(wget(64 + i) === golden[i], 64 + i);
        end

        upload(129, 5);
        repeat (400) @(negedge clk);
        for (i = 0; i < 5; i = i + 1) begin
            chk(wget(129 + i) === golden[i], 129 + i);
        end

        if (errors == 0) begin
            $display("PASS mag_1m_upload_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL mag_1m_upload_tb: %0d errors, %0d checks",
                     errors, checks);
        end
        $finish;
    end

    initial begin
        #500000;
        $display("FAIL mag_1m_upload_tb: watchdog (master AW=%0d W=%0d)",
                 aw_seen, w_seen);
        $finish;
    end
endmodule

`default_nettype wire
