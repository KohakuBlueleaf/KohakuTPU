// The platform proof, RTL edition: the GENERATED saxpy mesh (gen_mesh.py
// --tokens, only kohakuaccel sources + examples/saxpy) driven exactly the way
// a host drives the card — operands uploaded through S_AXI_MEM, the program
// staged and dispatched through the orchestrator's S_AXI_CTRL, completion
// seen in SIG_DONE/NODE_STATUS, the result read back through S_AXI_MEM.
`timescale 1ns / 1ps
`default_nettype none

module saxpy_mesh_tb;
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4, MW = 512;

    // the orchestrator map, control-registers.md s2.2
    localparam [31:0] A_STATUS = 32'h08, A_CAPS = 32'h10, A_PROG_DST = 32'h40;
    localparam [31:0] A_PROG_LEN = 32'h48, A_PROG_KICK = 32'h50;
    localparam [31:0] A_PROG_CRED = 32'h60, A_PROG_BASE = 32'h68;
    localparam [31:0] A_SIG_DONE = 32'h70, A_NODE = 32'h1000;
    localparam [31:0] A_STAGE = 32'h2000;

    reg clk = 0, rstn = 0;
    always begin
        #2 clk = ~clk;
    end

    // ---- S_AXI_MEM master (256-bit) ----
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

    // ---- S_AXI_CTRL master (64-bit) ----
    reg  [IDW-1:0] sc_awid = 0, sc_arid = 0;
    reg  [31:0]    sc_awaddr = 0, sc_araddr = 0;
    reg            sc_awvalid = 0, sc_arvalid = 0;
    reg  [63:0]    sc_wdata = 0;
    reg            sc_wvalid = 0;
    wire           sc_awready, sc_wready, sc_bvalid, sc_arready;
    wire [63:0]    sc_rdata;
    wire           sc_rvalid;

    // ---- M_AXI_DRAM (512-bit) into the model DRAM ----
    wire [IDW-1:0]  dm_awid, dm_arid, dm_bid, dm_rid;
    wire [AW-1:0]   dm_awaddr, dm_araddr;
    wire [7:0]      dm_awlen, dm_arlen;
    wire [2:0]      dm_awsize, dm_arsize;
    wire [1:0]      dm_awburst, dm_arburst, dm_bresp, dm_rresp;
    wire            dm_awvalid, dm_awready, dm_wvalid, dm_wready, dm_wlast;
    wire            dm_bvalid, dm_bready, dm_arvalid, dm_arready;
    wire            dm_rvalid, dm_rready, dm_rlast;
    wire [MW-1:0]   dm_wdata, dm_rdata;
    wire [MW/8-1:0] dm_wstrb;

    wire mv_busy;

    saxpy_mesh dut (
        .axi_aclk(clk), .axi_aresetn(rstn),
        .noc_clk(clk), .mat_clk(clk), .vec_clk(clk),
        .dram_aclk(clk), .dram_aresetn(rstn),
        .S_AXI_MEM_awid(sm_awid), .S_AXI_MEM_awaddr(sm_awaddr),
        .S_AXI_MEM_awlen(sm_awlen), .S_AXI_MEM_awvalid(sm_awvalid),
        .S_AXI_MEM_awready(sm_awready),
        .S_AXI_MEM_wdata(sm_wdata), .S_AXI_MEM_wstrb(sm_wstrb),
        .S_AXI_MEM_wlast(sm_wlast), .S_AXI_MEM_wvalid(sm_wvalid),
        .S_AXI_MEM_wready(sm_wready),
        .S_AXI_MEM_bid(), .S_AXI_MEM_bresp(), .S_AXI_MEM_bvalid(sm_bvalid),
        .S_AXI_MEM_bready(1'b1),
        .S_AXI_MEM_arid(sm_arid), .S_AXI_MEM_araddr(sm_araddr),
        .S_AXI_MEM_arlen(sm_arlen), .S_AXI_MEM_arvalid(sm_arvalid),
        .S_AXI_MEM_arready(sm_arready),
        .S_AXI_MEM_rid(), .S_AXI_MEM_rdata(sm_rdata), .S_AXI_MEM_rresp(),
        .S_AXI_MEM_rlast(sm_rlast), .S_AXI_MEM_rvalid(sm_rvalid),
        .S_AXI_MEM_rready(1'b1),
        .S_AXI_CTRL_awid(sc_awid), .S_AXI_CTRL_awaddr(sc_awaddr),
        .S_AXI_CTRL_awlen(8'd0), .S_AXI_CTRL_awvalid(sc_awvalid),
        .S_AXI_CTRL_awready(sc_awready),
        .S_AXI_CTRL_wdata(sc_wdata), .S_AXI_CTRL_wstrb(8'hFF),
        .S_AXI_CTRL_wlast(1'b1), .S_AXI_CTRL_wvalid(sc_wvalid),
        .S_AXI_CTRL_wready(sc_wready),
        .S_AXI_CTRL_bid(), .S_AXI_CTRL_bresp(), .S_AXI_CTRL_bvalid(sc_bvalid),
        .S_AXI_CTRL_bready(1'b1),
        .S_AXI_CTRL_arid(sc_arid), .S_AXI_CTRL_araddr(sc_araddr),
        .S_AXI_CTRL_arlen(8'd0), .S_AXI_CTRL_arvalid(sc_arvalid),
        .S_AXI_CTRL_arready(sc_arready),
        .S_AXI_CTRL_rid(), .S_AXI_CTRL_rdata(sc_rdata), .S_AXI_CTRL_rresp(),
        .S_AXI_CTRL_rlast(), .S_AXI_CTRL_rvalid(sc_rvalid),
        .S_AXI_CTRL_rready(1'b1),
        .M_AXI_DRAM_awid(dm_awid), .M_AXI_DRAM_awaddr(dm_awaddr),
        .M_AXI_DRAM_awlen(dm_awlen), .M_AXI_DRAM_awsize(dm_awsize),
        .M_AXI_DRAM_awburst(dm_awburst),
        .M_AXI_DRAM_awvalid(dm_awvalid), .M_AXI_DRAM_awready(dm_awready),
        .M_AXI_DRAM_wdata(dm_wdata), .M_AXI_DRAM_wstrb(dm_wstrb),
        .M_AXI_DRAM_wlast(dm_wlast), .M_AXI_DRAM_wvalid(dm_wvalid),
        .M_AXI_DRAM_wready(dm_wready),
        .M_AXI_DRAM_bid(dm_bid), .M_AXI_DRAM_bresp(dm_bresp),
        .M_AXI_DRAM_bvalid(dm_bvalid), .M_AXI_DRAM_bready(dm_bready),
        .M_AXI_DRAM_arid(dm_arid), .M_AXI_DRAM_araddr(dm_araddr),
        .M_AXI_DRAM_arlen(dm_arlen), .M_AXI_DRAM_arsize(dm_arsize),
        .M_AXI_DRAM_arburst(dm_arburst),
        .M_AXI_DRAM_arvalid(dm_arvalid), .M_AXI_DRAM_arready(dm_arready),
        .M_AXI_DRAM_rid(dm_rid), .M_AXI_DRAM_rdata(dm_rdata),
        .M_AXI_DRAM_rresp(dm_rresp), .M_AXI_DRAM_rlast(dm_rlast),
        .M_AXI_DRAM_rvalid(dm_rvalid), .M_AXI_DRAM_rready(dm_rready)
    );

    axi_ram #(.DATA_W(MW), .ADDR_W(AW), .ID_W(IDW), .WORDS(2048),
              .PORTS(1)) u_ram (
        .clk(clk), .resetn(rstn),
        .s_awid(dm_awid), .s_awaddr(dm_awaddr), .s_awlen(dm_awlen),
        .s_awsize(dm_awsize), .s_awburst(dm_awburst),
        .s_awvalid(dm_awvalid), .s_awready(dm_awready),
        .s_wdata(dm_wdata), .s_wstrb(dm_wstrb), .s_wlast(dm_wlast),
        .s_wvalid(dm_wvalid), .s_wready(dm_wready),
        .s_bid(dm_bid), .s_bresp(dm_bresp), .s_bvalid(dm_bvalid),
        .s_bready(dm_bready),
        .s_arid(dm_arid), .s_araddr(dm_araddr), .s_arlen(dm_arlen),
        .s_arsize(dm_arsize), .s_arburst(dm_arburst),
        .s_arvalid(dm_arvalid), .s_arready(dm_arready),
        .s_rid(dm_rid), .s_rdata(dm_rdata), .s_rresp(dm_rresp),
        .s_rlast(dm_rlast), .s_rvalid(dm_rvalid), .s_rready(dm_rready),
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({MW{1'b0}}), .bd_rdata()
    );

    // exact whole-value float32, the domain the datapath contract names
    function [31:0] i2f(input signed [31:0] v);
        reg [31:0] mag, sh;
        reg [7:0]  e;
        integer    i, p;
        begin
            mag = v[31] ? -v : v;
            p = 0;
            for (i = 0; i < 24; i = i + 1) begin
                if (mag[i]) begin
                    p = i;
                end
            end
            e  = 8'd127 + p;
            sh = mag << (23 - p);
            i2f = (mag == 32'd0) ? 32'd0 : {v[31], e, sh[22:0]};
        end
    endfunction

    integer errors = 0, checks = 0, spin;
    task chk(input [63:0] got, input [63:0] want, input [511:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                $display("%0t ERROR %0s: got %0h want %0h", $time, what, got, want);
            end
        end
    endtask

    // ---- host tasks, the driver's access pattern ----
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

    reg [63:0] cr_data;
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

    task mwr(input [AW-1:0] a, input [DW-1:0] d);
        begin
            @(negedge clk);
            sm_awaddr = a; sm_awvalid = 1;
            while (!sm_awready) begin
                @(negedge clk);
            end
            @(negedge clk); sm_awvalid = 0;
            sm_wdata = d; sm_wstrb = {DW/8{1'b1}}; sm_wlast = 1; sm_wvalid = 1;
            while (!sm_wready) begin
                @(negedge clk);
            end
            @(negedge clk); sm_wvalid = 0; sm_wlast = 0;
            while (!sm_bvalid) begin
                @(negedge clk);
            end
            @(negedge clk);
        end
    endtask

    reg [DW-1:0] mr_data;
    task mrd(input [AW-1:0] a);
        begin
            @(negedge clk);
            sm_araddr = a; sm_arvalid = 1;
            while (!sm_arready) begin
                @(negedge clk);
            end
            @(negedge clk); sm_arvalid = 0;
            while (!sm_rvalid) begin
                @(negedge clk);
            end
            mr_data = sm_rdata;
            @(negedge clk);
        end
    endtask

    // POLLED, as mm_mesh_tb learned: the completion retires when the last WR
    // beat is SENT, and the port's write engine can issue the DRAM write after
    // the host's read of the same word (memory-protocol.md s7.2 orders none of
    // this). A host reading back immediately must retry.
    task mrd_expect(input [AW-1:0] a, input [DW-1:0] want, input [511:0] what);
        begin
            mrd(a);
            spin = 0;
            while (mr_data !== want && spin < 2000) begin
                spin = spin + 1;
                mrd(a);
            end
            chk(mr_data, want, what);
        end
    endtask

    // stage one flit: five 64-bit words at 0x2000 + slot*40, low word first
    task stage_flit(input [7:0] slot, input [FW-1:0] f);
        begin
            cwr(A_STAGE + {24'd0, slot} * 40 + 0,  f[63:0]);
            cwr(A_STAGE + {24'd0, slot} * 40 + 8,  f[127:64]);
            cwr(A_STAGE + {24'd0, slot} * 40 + 16, f[191:128]);
            cwr(A_STAGE + {24'd0, slot} * 40 + 24, f[255:192]);
            cwr(A_STAGE + {24'd0, slot} * 40 + 32, {32'd0, f[287:256]});
        end
    endtask

    // dst/src are don't-care: the dispatcher rewrites the routing header
    function [FW-1:0] inst_flit(input [7:0] txn, input last,
                                input [255:0] payload);
        inst_flit = {16'd0, 4'h5 /*CU_INST*/, txn, last, 3'b000, payload};
    endfunction

    // field-for-field driver/examples/saxpy/sw/isa.py
    function [255:0] saxpy_inst(input [23:0] n, input [31:0] a_bits,
                                input [63:0] xa, input [63:0] ya);
        saxpy_inst = {8'h01, n, a_bits, xa, ya, 64'd0};
    endfunction

    task dispatch(input [7:0] dst_yx, input [15:0] base, input [15:0] len,
                  input [15:0] cred);
        begin
            cwr(A_PROG_BASE, {48'd0, base});
            cwr(A_PROG_LEN, {48'd0, len});
            cwr(A_PROG_DST, {56'd0, dst_yx});
            cwr(A_PROG_CRED, {48'd0, cred});
            cwr(A_PROG_KICK, 64'd1);
        end
    endtask

    task wait_done(input [31:0] want);
        begin
            spin = 0;
            cr_data = 0;
            while (cr_data < {32'd0, want} && spin < 4000) begin
                crd(A_SIG_DONE);
                spin = spin + 1;
            end
            chk(cr_data >= {32'd0, want}, 1, "SIG_DONE reached the target");
        end
    endtask

    integer i, s;
    reg [255:0] line, exp;
    initial begin
        for (i = 0; i < 2048; i = i + 1) begin
            u_ram.mem[i] = {MW{1'b0}};
        end
        repeat (20) @(negedge clk);
        rstn = 1;
        repeat (20) @(negedge clk);

        // 1. the control plane answers: CAPS says what was generated
        crd(A_CAPS);
        chk(cr_data, 64'h0000_0001_0104_0120, "CAPS: FW 288, PW 4, grid 1..1");
        crd(A_STATUS);
        chk(cr_data[2], 1'b1, "STATUS mesh_ready");

        // 2. host upload: x at 0x400, y at 0x800, n=16 whole-valued floats
        for (s = 0; s < 8; s = s + 1) begin
            line[s*32 +: 32] = i2f(s - 5);
        end
        mwr(40'h400, line);
        for (s = 0; s < 8; s = s + 1) begin
            line[s*32 +: 32] = i2f(8 + s - 5);
        end
        mwr(40'h420, line);
        for (s = 0; s < 8; s = s + 1) begin
            line[s*32 +: 32] = i2f(200 - 3*s);
        end
        mwr(40'h800, line);
        for (s = 0; s < 8; s = s + 1) begin
            line[s*32 +: 32] = i2f(200 - 3*(8 + s));
        end
        mwr(40'h820, line);

        // 2b. upload round-trip before any compute touches it
        for (s = 0; s < 8; s = s + 1) begin
            exp[s*32 +: 32] = i2f(200 - 3*(8 + s));
        end
        mrd(40'h820);
        chk(mr_data, exp, "upload round-trip, y line 1");

        // 3. one saxpy program to the unit at (1,0): y = 2*x + y
        cwr(A_SIG_DONE, 64'd0);
        stage_flit(8'd0, inst_flit(8'h42, 1'b1,
                   saxpy_inst(24'd16, i2f(2), 64'h400, 64'h800)));
        dispatch(8'h01 /*{y0,x1}*/, 16'd0, 16'd1, 16'd4);
        wait_done(1);

        // 4. the result, read back the way the host reads it
        for (i = 0; i < 2; i = i + 1) begin
            for (s = 0; s < 8; s = s + 1) begin
                exp[s*32 +: 32] = i2f(2*(i*8 + s - 5) + 200 - 3*(i*8 + s));
            end
            mrd_expect(40'h800 + i*32, exp, "unit0 y line");
        end

        // 5. the mirror: a batch report from (1,0), program id echoed
        crd(A_NODE + 32'h8);          // NODE_STATUS[{0,1}]
        chk(cr_data[63:56], 8'h01, "node (1,0) reported SIG_BATCH");
        chk(cr_data[55:24], 32'h42, "batch arg is the program id");
        chk(cr_data[0], 1'b1, "node (1,0) valid");

        // 6. the second unit at (1,2), staged at PROG_BASE 1: y2 = 3*x + y2
        for (s = 0; s < 8; s = s + 1) begin
            line[s*32 +: 32] = i2f(100 + s);
        end
        mwr(40'hC00, line);
        cwr(A_SIG_DONE, 64'd0);
        stage_flit(8'd1, inst_flit(8'h77, 1'b1,
                   saxpy_inst(24'd8, i2f(3), 64'h400, 64'hC00)));
        dispatch(8'h21 /*{y2,x1}*/, 16'd1, 16'd1, 16'd4);
        wait_done(1);
        for (s = 0; s < 8; s = s + 1) begin
            exp[s*32 +: 32] = i2f(4*s + 85);
        end
        mrd_expect(40'hC00, exp, "unit1 y line");
        crd(A_NODE + 32'h8 * 8'h21);  // NODE_STATUS[{2,1}]
        chk(cr_data[63:56], 8'h01, "node (1,2) reported SIG_BATCH");
        chk(cr_data[55:24], 32'h77, "batch arg is the program id");

        // 7. x untouched by either run
        for (s = 0; s < 8; s = s + 1) begin
            exp[s*32 +: 32] = i2f(s - 5);
        end
        mrd(40'h400);
        chk(mr_data, exp, "x operand untouched");

        if (errors == 0) begin
            $display("PASS saxpy_mesh_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL saxpy_mesh_tb: %0d errors", errors);
        end
        $finish;
    end

    initial begin
        #2000000;
        $display("FAIL saxpy_mesh_tb: watchdog");
        $finish;
    end
endmodule

`default_nettype wire
