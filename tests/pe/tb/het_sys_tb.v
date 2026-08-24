// het_sys_tb -- the heterogeneous mesh, driven the way a host drives the card.
//
// No agent on the NoC: the only way in is MAG's two AXI slaves, S_AXI_CTRL for
// the orchestrator and S_AXI_MEM for memory. This bench is the host.
//
// STEP ONE ONLY, so far: does the control plane answer, and do all four PEs
// come out of reset idle rather than X. Loading images and kicking comes next
// and needs the CU_DATA/CU_INST flits staged through the orchestrator.
//
//   python scripts/py/xsim.py het_sys --max-time 40us

`default_nettype none
`timescale 1ns/1ps

module het_sys_tb;
    localparam integer FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4;

    // the orchestrator map, control-registers.md s2.2
    localparam [31:0] A_CTRL = 32'h00, A_STATUS = 32'h08, A_CAPS = 32'h10;

    reg clk = 1'b0, rstn = 1'b0;
    always begin
        #2 clk = ~clk;
    end

    integer errors = 0, checks = 0;
    task chk(input [63:0] got, input [63:0] want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                $display("  FAIL %0s: got %h want %h", what, got, want);
                errors = errors + 1;
            end else begin
                $display("  ok   %0s = %h", what, got);
            end
        end
    endtask

    // ---- S_AXI_CTRL master, 64-bit ----
    reg  [IDW-1:0] sc_awid = 0, sc_arid = 0;
    reg  [31:0]    sc_awaddr = 0, sc_araddr = 0;
    reg            sc_awvalid = 0, sc_arvalid = 0;
    reg  [63:0]    sc_wdata = 0;
    reg            sc_wvalid = 0;
    wire           sc_awready, sc_wready, sc_bvalid, sc_arready;
    wire [63:0]    sc_rdata;
    wire           sc_rvalid;

    // ---- S_AXI_MEM master, 256-bit ----
    reg  [IDW-1:0] sm_awid = 0, sm_arid = 0;
    reg  [AW-1:0]  sm_awaddr = 0, sm_araddr = 0;
    reg  [7:0]     sm_awlen = 0, sm_arlen = 0;
    reg            sm_awvalid = 0, sm_arvalid = 0;
    reg  [DW-1:0]  sm_wdata = 0;
    reg  [DW/8-1:0] sm_wstrb = 0;
    reg            sm_wlast = 0, sm_wvalid = 0;
    wire           sm_awready, sm_wready, sm_bvalid, sm_arready;
    wire [DW-1:0]  sm_rdata;
    wire           sm_rvalid, sm_rlast;

    wire [3:0] pe_run, pe_halted, pe_busy;

    het_mesh #(.FW(FW), .PW(PW), .DW(DW), .AW(AW), .IDW(IDW)) dut (
        .clk(clk), .rstn(rstn),
        .sm_awid(sm_awid), .sm_awaddr(sm_awaddr), .sm_awlen(sm_awlen),
        .sm_awvalid(sm_awvalid), .sm_awready(sm_awready),
        .sm_wdata(sm_wdata), .sm_wstrb(sm_wstrb), .sm_wlast(sm_wlast),
        .sm_wvalid(sm_wvalid), .sm_wready(sm_wready),
        .sm_bvalid(sm_bvalid), .sm_bready(1'b1),
        .sm_arid(sm_arid), .sm_araddr(sm_araddr), .sm_arlen(sm_arlen),
        .sm_arvalid(sm_arvalid), .sm_arready(sm_arready),
        .sm_rdata(sm_rdata), .sm_rlast(sm_rlast), .sm_rvalid(sm_rvalid),
        .sm_rready(1'b1),
        .sc_awid(sc_awid), .sc_awaddr(sc_awaddr), .sc_awvalid(sc_awvalid),
        .sc_awready(sc_awready),
        .sc_wdata(sc_wdata), .sc_wvalid(sc_wvalid), .sc_wready(sc_wready),
        .sc_bvalid(sc_bvalid), .sc_bready(1'b1),
        .sc_arid(sc_arid), .sc_araddr(sc_araddr), .sc_arvalid(sc_arvalid),
        .sc_arready(sc_arready),
        .sc_rdata(sc_rdata), .sc_rvalid(sc_rvalid), .sc_rready(1'b1),
        .pe_run(pe_run), .pe_halted(pe_halted), .pe_busy(pe_busy)
    );

    reg [63:0] cr_data;

    // ADDRESS THEN DATA, never both at once. Driving AW and W together and
    // waiting on `awready && wready` deadlocks the moment the slave accepts the
    // write address before it accepts the beat -- which is what MAG does.
    task cwr(input [31:0] a, input [63:0] d);
        begin
            @(negedge clk);
            sc_awaddr = a; sc_awvalid = 1;
            while (!sc_awready) begin
                @(negedge clk);
            end
            @(negedge clk); sc_awvalid = 0;
            sc_wdata = d; sc_wvalid = 1;
            while (!sc_wready) begin
                @(negedge clk);
            end
            @(negedge clk); sc_wvalid = 0;
            while (!sc_bvalid) begin
                @(negedge clk);
            end
            @(negedge clk);
        end
    endtask

    task crd(input [31:0] a);
        begin
            @(negedge clk);
            sc_araddr = a; sc_arvalid = 1;
            while (!sc_arready) begin
                @(negedge clk);
            end
            @(negedge clk); sc_arvalid = 0;
            while (!sc_rvalid) begin
                @(negedge clk);
            end
            cr_data = sc_rdata;
            @(negedge clk);
        end
    endtask

    initial begin
        $display("--- het mesh: 1 router, 1 MAG, CPU + GPU + 2 DSP, host on AXI ---");
        repeat (20) @(negedge clk);
        rstn = 1;
        repeat (40) @(negedge clk);

        // The control plane is reachable and the mesh reports itself ready.
        crd(A_CAPS);
        $display("  CAPS   = %h", cr_data);
        crd(A_STATUS);
        $display("  STATUS = %h", cr_data);
        chk(cr_data[2], 1'b1, "STATUS mesh_ready");

        // Nothing has been kicked, so every PE must be idle and DEFINED. An X
        // here is a PE that never left reset, which a run-count check would miss.
        repeat (200) @(negedge clk);
        chk({28'd0, pe_run},    32'd0, "no PE is running");
        chk({28'd0, pe_halted}, 32'd0, "no PE has halted");
        chk((^pe_busy === 1'bx) ? 1'b1 : 1'b0, 1'b0, "pe_busy is defined");

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
        #400000;
        $display("  FAIL -- watchdog: the control plane never answered");
        $finish;
    end

endmodule

`default_nettype wire
