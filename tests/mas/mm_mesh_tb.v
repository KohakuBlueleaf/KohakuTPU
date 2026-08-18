// The minimal machine, end to end: 1 MAG, 1 cluster, 1 vector core, 2 routers.
//
// The bench is the agent. It injects CU_INST flits at r11's local port and
// reads the AXI RAM through its backdoor, so every path exercised here is the
// real one: NoC routing, MAG's memory ports, the memory mover on its own AXI
// master, and both compute units.
//
// Three things are proven, and they are deliberately different paths:
//
//   1. the MOVER moves a region                MAG's new AXI master
//   2. the VECTOR CORE computes and drains     NoC -> vec -> NoC -> MAG write
//   3. the CLUSTER fetches operands            NoC -> MAG read -> NoC
//
// The vector kernel deliberately needs no VFILL: it builds its data from a
// scalar broadcast. See the note at part 2 -- VFILL against real MAG does not
// work yet, and this bench is what established that.

`default_nettype none
`timescale 1ns/1ps

module mm_mesh_tb #(
    // HALF periods in ns, against the mesh's 2.0. One rate per TYPE, so the
    // cluster and the vector core are separate domains, not one shared one.
    parameter integer UNIT_CDC = 0,
    // MAG is an endpoint too: its own rate, behind a CDC on its NoC port.
    parameter integer MAG_CDC  = 0,
    // 1 derives `mat_clk` from `mat_clk2x` instead of driving it free, so the
    // pumped pair is edge-aligned the way BUFGCE_DIV(1)/(2) makes it.
    parameter integer PUMP     = 0,
    parameter real    MHP      = 2.0,
    parameter real    M2HP     = 1.0,
    parameter real    GHP      = 2.0,
    parameter real    VHP      = 2.0,
    // MAG staging on the converged path, so the async unit clocks and the L2
    // are exercised in one design rather than in two separate benches.
    parameter integer L2MAG    = 0
);
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4;
    localparam MEMP = 1, NCH = MEMP + 2;

    localparam [3:0] T_CU_INST = 4'h5;
    // We sit on r11's LOCAL port, so our coordinate is the router's own. Any
    // other value routes a CU_SIGNAL off the edge of a two-router mesh and it
    // is silently dropped.
    localparam CX = 1, CY = 1;

    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

    reg mat_free = 0, vec_clk = 0, mag_clk = 0;
    always #MHP mat_free = ~mat_free;
    always #VHP vec_clk  = ~vec_clk;
    always #GHP mag_clk  = ~mag_clk;

    // ktpu_div2's own model of the primitive: O rises WITH an I edge, never a
    // delta after, or every 1x register samples what 2x has already updated.
    reg mat_clk2x = 0, mph = 0;
    always #M2HP mat_clk2x = ~mat_clk2x;
    always @(negedge mat_clk2x) mph <= ~mph;
    wire mat_clk = PUMP ? (mat_clk2x & mph) : mat_free;

    reg  [FW-1:0] ext_i;
    reg           ext_iv;
    reg  [255:0]  prog_mem [0:15];   // compiler-emitted payloads, see PROG_HEX
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

    // ONE channel: MAG converges its internal requesters itself now.
    wire [IDW-1:0]  r_awid, r_arid, r_bid, r_rid;
    wire [AW-1:0]   r_awaddr, r_araddr;
    wire [7:0]      r_awlen, r_arlen;
    wire [2:0]      r_awsize, r_arsize;
    wire [1:0]      r_awburst, r_arburst, r_bresp, r_rresp;
    wire            r_awvalid, r_awready, r_wvalid, r_wready, r_wlast;
    wire            r_bvalid, r_bready, r_arvalid, r_arready;
    wire            r_rvalid, r_rready, r_rlast;
    wire [DW-1:0]   r_wdata, r_rdata;
    wire [DW/8-1:0] r_wstrb;

    wire [47:0] dbg_cluster;
    wire [31:0] dbg_vcyc, obs;
    wire        dbg_vflt;

    mm_mesh #(.FW(FW), .PW(PW), .DW(DW), .AW(AW), .IDW(IDW), .MEMP(MEMP),
              .UNIT_CDC(UNIT_CDC), .MAG_CDC(MAG_CDC), .PUMP(PUMP), .MODEL(1),
              .L2_MAG(L2MAG), .L2_MAG_BANKS(4), .L2_MAG_ENTRIES(1024),
              .L2_MAG_AT_PORT(L2MAG)) dut (
        .clk(clk), .mag_clk(mag_clk), .mat_clk(mat_clk),
        .mat_clk2x(mat_clk2x), .vec_clk(vec_clk), .rst(rst),
        .sm_awaddr({AW{1'b0}}), .sm_awlen(8'd0), .sm_awvalid(1'b0),
        .sm_wdata({DW{1'b0}}), .sm_wlast(1'b0), .sm_wvalid(1'b0),
        .sc_awaddr(sc_awaddr), .sc_awvalid(sc_awvalid), .sc_awready(sc_awready),
        .sc_wdata(sc_wdata), .sc_wvalid(sc_wvalid), .sc_wready(sc_wready),
        .sc_bvalid(sc_bvalid),
        .sc_araddr(32'd0), .sc_arvalid(1'b0),
        .sc_rdata(), .sc_rvalid(),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done),
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

    reg          bd_we = 0;
    reg  [15:0]  bd_addr = 0;
    reg  [DW-1:0] bd_wdata = 0;
    wire [DW-1:0] bd_rdata;

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .WORDS(2048),
              .PORTS(1)) u_ram (
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

    // ---- the bench as the agent: inject one CU_INST -----------------------
    task send_inst(input [3:0] dx, input [3:0] dy, input [255:0] payload);
        begin
            @(negedge clk);
            while (ext_ib) @(negedge clk);
            ext_i  <= {dx, dy, CX[3:0], CY[3:0], T_CU_INST, 8'h40, 1'b0, 3'b000,
                       payload};
            ext_iv <= 1'b1;
            @(negedge clk);
            ext_iv <= 1'b0;
        end
    endtask

    // vector core is at (1,0); the cluster manager at (2,1)
    task vec_inst(input [255:0] p); begin send_inst(4'd1, 4'd0, p); end endtask
    task cl_inst (input [255:0] p); begin send_inst(4'd2, 4'd1, p); end endtask

    task put_imem(input [8:0] a, input [31:0] w);
        begin vec_inst({4'd1, a, 211'd0, w}); end
    endtask
    task put_desc(input [2:0] ad, input [2:0] f, input [33:0] v);
        begin vec_inst({4'd2, ad, f, v, 212'd0}); end
    endtask

    // ---- mover configuration ---------------------------------------------
    // Through S_AXI_CTRL at the orchestrator's A_AUX_CFG window, which is the
    // only path a block design can wire -- so this bench now exercises the one
    // the card will use. `a` is still the mover's own register offset.
    localparam [31:0] A_MV_CFG = 32'h0800;
    // MAG's AXI slave lives in MAG's domain, so the host drives it there. The
    // real BD needs an axi_clock_converter here for the same reason.
    wire host_clk = MAG_CDC ? mag_clk : clk;

    task mvwr(input [7:0] a, input [63:0] d);
        begin
            @(negedge host_clk);
            sc_awaddr = A_MV_CFG + {24'd0, a}; sc_awvalid = 1'b1;
            while (!sc_awready) @(negedge host_clk);
            @(negedge host_clk);
            sc_awvalid = 1'b0;
            sc_wdata = d; sc_wvalid = 1'b1;
            while (!sc_wready) @(negedge host_clk);
            @(negedge host_clk);
            sc_wvalid = 1'b0;
            while (!sc_bvalid) @(negedge host_clk);
            @(negedge host_clk);
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

    integer i, sig_count;

    // count CU_SIGNALs coming back so completion is observable
    always @(posedge clk) begin
        if (rst) sig_count <= 0;
        else if (ext_ov && ext_o[FW-4*PW-1 -: 4] == 4'h6)
            sig_count <= sig_count + 1;
    end

    // The full round trip: VFILL pulls a word out of DRAM through MAG, VLD
    // converts it into the lanes, VADD doubles it, VST puts it back in L1 and
    // VDRAIN writes it to DRAM. Both directions against the real memory port.
    localparam [31:0] I_VSETI  = 32'hD0000000;
    localparam [31:0] I_VSETVL = 32'hC0000000;
    localparam [31:0] I_VSETMD = 32'hC8000000;
    localparam [31:0] I_VFILL  = 32'hE8000000;   // A0 -> L1 word 0
    localparam [31:0] I_VBAR   = 32'hE0000000;
    localparam [31:0] I_VLD    = 32'hA1200000;   // v0 <- L1 via A1
    localparam [31:0] I_VADD   = 32'h18020000;   // v1 = v0 + v0
    localparam [31:0] I_VST    = 32'hA9620000;   // v1 -> L1 via A3
    localparam [31:0] I_VDRAIN = 32'hF0400001;   // L1 word 1 -> DRAM via A2
    localparam [31:0] I_VHALT  = 32'hF8000000;

    localparam [33:0] A_DST  = 34'h8000;   // word 1024
    localparam [33:0] A_VSRC = 34'h9000;   // word 1152, the vector operand
    localparam [33:0] A_MSRC = 34'h1000;   // word 128
    localparam [33:0] A_MDST = 34'h2000;   // word 256

    initial begin
        ext_i = 0; ext_iv = 0;
        for (i = 0; i < 2048; i = i + 1) u_ram.mem[i] = 256'd0;
        for (i = 0; i < 16; i = i + 1)
            u_ram.mem[(A_MSRC >> 5) + i] = {8{32'h7E00_0000 | i[31:0]}};
        u_ram.mem[A_VSRC >> 5] = {16{16'h4000}};      // 2.0, sixteen FP16 lanes

        repeat (10) @(negedge clk);
        rst = 0;
        repeat (10) @(negedge clk);

        // ============ 1. the mover, inside the assembled machine ============
        $display("--- 1. mover: a 4x4 word transpose through MAG ---");
        mvhdr(1'b0, A_MSRC, 3'd2);
        mvdim(1'b0, 3'd0, 16'd4, 32'sd128);
        mvdim(1'b0, 3'd1, 16'd4, 32'sd32);
        mvhdr(1'b1, A_MDST, 3'd2);
        mvdim(1'b1, 3'd0, 16'd4, 32'sd32);
        mvdim(1'b1, 3'd1, 16'd4, 32'sd128);
        mvwr(8'h00, {47'd0, 1'b1, 8'd0, 3'd0, 2'd1, 3'd0});
        // Wait for the START, not one edge: `mv_busy` is raised in MAG's domain,
        // so at any other rate the old single edge polled before it asserted.
        spin = 0;
        while (!mv_busy && spin < 10000) begin spin = spin + 1; @(negedge clk); end
        chk(spin < 10000, "mover started", 0);
        spin = 0;
        while (mv_busy && spin < 100000) begin spin = spin + 1; @(negedge clk); end
        chk(spin < 100000, "mover went idle", 0);
        chk(mv_fault === 4'd0, "mover did not fault", 0);
        for (i = 0; i < 16; i = i + 1) begin
            @(negedge clk); bd_addr = (A_MDST >> 5) + i[15:0];
            @(negedge clk); @(negedge clk);
            chk(bd_rdata === {8{32'h7E00_0000 | ((i%4)*4 + (i/4))}},
                "transposed word", i);
        end

        // ============ 2. the vector core, over the real NoC ============
        $display("--- 2. vector core: staged over the NoC, drained to DRAM ---");
        put_imem(9'd0,  I_VSETI);   put_imem(9'd1, 32'd16);
        put_imem(9'd2,  I_VSETVL);
        put_imem(9'd3,  I_VSETMD);
        put_imem(9'd4,  I_VFILL);
        put_imem(9'd5,  I_VBAR);
        put_imem(9'd6,  I_VLD);
        put_imem(9'd7,  I_VADD);
        put_imem(9'd8,  I_VST);
        put_imem(9'd9,  I_VDRAIN);
        put_imem(9'd10, I_VHALT);

        put_desc(3'd0, 3'd0, A_VSRC);                // A0: the DRAM operand
        put_desc(3'd0, 3'd1, {18'd32, 16'd1});
        put_desc(3'd1, 3'd0, 34'd0);                 // A1: L1 word 0
        put_desc(3'd1, 3'd1, {18'd1, 16'd1});
        put_desc(3'd2, 3'd0, A_DST);                 // A2: DRAM destination
        put_desc(3'd2, 3'd1, {18'd32, 16'd1});
        put_desc(3'd3, 3'd0, 34'd1);                 // A3: L1 word 1
        put_desc(3'd3, 3'd1, {18'd1, 16'd1});

        vec_inst({4'd3, 9'd0, 243'd0});               // RUN at pc 0
        spin = 0;
        while ((sig_count < 20) && (spin < 400000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(spin < 400000, "vector kernel retired", 0);
        chk(dbg_vflt === 1'b0, "vector core did not fault", 0);

        @(negedge clk); bd_addr = A_DST >> 5;
        // POLLED, not a fixed wait: the write crosses MAG's one dram port and
        // can still be in its write FIFO when the kernel retires.
        spin = 0;
        while ((bd_rdata !== {16{16'h4400}}) && (spin < 4000)) begin
            spin = spin + 1; @(negedge clk);
        end
        // 2.0 fetched from DRAM, doubled, written back as FP16 4.0
        chk(bd_rdata === {16{16'h4400}}, "vector result reached DRAM", 0);

        // ============ 3. the cluster fetching through MAG ============
        $display("--- 3. cluster: a FILL served by MAG over the mesh ---");
        // op=1 FILL, addr=A_MSRC, n=1 entry, sel=A, quantise on the way out
`ifdef PROG_HEX
        // THE COMPILER'S OWN BITS. gen_prog.py writes what kohakutpu.hw.matmul
        // emits, so an encoder that drifts from the RTL fails here, not on card.
        // xsim.py runs from build/xsim_<bench>, so the repo root is two up.
        $readmemh("../../build/prog/mm_mesh_fill.hex", prog_mem);
        if (prog_mem[0] === {256{1'bx}}) begin
            $display("FAIL mm_mesh: PROG_HEX set but build/prog/mm_mesh_fill.hex is missing");
            $finish;
        end
        cl_inst(prog_mem[0]);
`else
        cl_inst({4'd1, A_MSRC, 16'd1, 1'b0, 1'b0, 8'd0, 8'd0, 8'd0, 8'd0,
                 24'd0, 2'd0, 1'b0, 8'd0, 8'd0, 8'd0, 1'b0, 1'b0, 115'd0});
`endif
        spin = 0;
        while ((dbg_cluster[47:32] == 16'd0) && (spin < 200000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(spin < 200000, "cluster FILL completed", 0);

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #6000000;
        $display("  FAIL -- watchdog (sig=%0d mvdone=%0d cl=%h)",
                 sig_count, mv_done, dbg_cluster);
        $finish;
    end

endmodule

`default_nettype wire
