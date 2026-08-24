// End-to-end test of the orchestrator: an AXI master drives the register map,
// the orchestrator injects into a real 2x2 mesh, and a loopback node at the far
// corner turns each arriving flit around so it comes back through RX.
//
// This is deliberately not a unit test of the register file. The thing worth
// proving is that a host can put a flit on the mesh and get one back using only
// documented registers -- which is exactly what bring-up will do from Tcl.
//
// Run: tests/run_noc_sim.ps1 -Orchestrator

`timescale 1ns/1ps

module noc_orchestrator_tb;

    localparam DW   = 64;      // AXI
    localparam FW   = 288;     // flit
    localparam POSW = 4;
    localparam LO   = 1, HI = 2;          // 2x2 router grid
    localparam WORDS = 5;                 // 288 bits over 64-bit AXI

    // register offsets, from docs/noc/spec.md s10.2
    localparam A_CAPS = 16'h0010, A_TX_FLIT0 = 16'h0100, A_TX_KICK = 16'h0140;
    localparam A_TX_STATUS = 16'h0148, A_RX_FLIT0 = 16'h0180;
    localparam A_RX_POP = 16'h01C0, A_RX_STATUS = 16'h01C8, A_NODE = 16'h1000;
    localparam A_PROG_DST = 16'h0040, A_PROG_LEN = 16'h0048;
    localparam A_PROG_KICK = 16'h0050, A_PROG_STAT = 16'h0058;
    localparam A_PROG_CRED = 16'h0060;

    reg clk = 0, resetn = 0;
    always begin
        #5 clk = ~clk;
    end

    integer errors = 0, checks = 0;

    // ---------------------------------------------------------------- AXI wires
    reg  [3:0]  awid=0, arid=0;
    reg  [31:0] awaddr=0, araddr=0;
    reg  [7:0]  awlen=0, arlen=0;
    reg         awvalid=0, arvalid=0, wvalid=0, wlast=0, bready=0, rready=0;
    reg  [DW-1:0]   wdata=0;
    reg  [DW/8-1:0] wstrb={(DW/8){1'b1}};
    wire awready, wready, arready, bvalid, rvalid, rlast;
    wire [3:0] bid, rid;
    wire [1:0] bresp, rresp;
    wire [DW-1:0] rdata;

    // ------------------------------------------------------- orchestrator + mesh
    wire [FW-1:0] orc_out_data, orc_in_data;
    wire          orc_out_valid, orc_in_valid, orc_out_busy, orc_in_busy;

    noc_orchestrator #(
        .DATA_WIDTH(DW), .ADDR_WIDTH(32), .ID_WIDTH(4), .FLIT_WIDTH(FW),
        .POS_WIDTH(POSW), .GRID_LO(LO), .GRID_HI(HI)
    ) dut (
        .clk(clk), .resetn(resetn),
        .s_axi_awid(awid), .s_axi_awaddr(awaddr), .s_axi_awlen(awlen),
        .s_axi_awsize(3'd3), .s_axi_awburst(2'b01),
        .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(wstrb), .s_axi_wlast(wlast),
        .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bid(bid), .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_arid(arid), .s_axi_araddr(araddr), .s_axi_arlen(arlen),
        .s_axi_arsize(3'd3), .s_axi_arburst(2'b01),
        .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rid(rid), .s_axi_rdata(rdata), .s_axi_rresp(rresp),
        .s_axi_rlast(rlast), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .noc_out_data(orc_out_data), .noc_out_valid(orc_out_valid),
        .noc_out_busy(orc_out_busy),
        .noc_in_data(orc_in_data), .noc_in_valid(orc_in_valid),
        .noc_in_busy(orc_in_busy)
    );

    // mesh links, [x][y]
    wire [FW-1:0] n_o_d[LO:HI][LO:HI], e_o_d[LO:HI][LO:HI], s_o_d[LO:HI][LO:HI],
                  w_o_d[LO:HI][LO:HI], l_o_d[LO:HI][LO:HI];
    wire          n_o_v[LO:HI][LO:HI], e_o_v[LO:HI][LO:HI], s_o_v[LO:HI][LO:HI],
                  w_o_v[LO:HI][LO:HI], l_o_v[LO:HI][LO:HI];
    wire          n_o_b[LO:HI][LO:HI], e_o_b[LO:HI][LO:HI], s_o_b[LO:HI][LO:HI],
                  w_o_b[LO:HI][LO:HI], l_o_b[LO:HI][LO:HI];
    wire [FW-1:0] n_i_d[LO:HI][LO:HI], e_i_d[LO:HI][LO:HI], s_i_d[LO:HI][LO:HI],
                  w_i_d[LO:HI][LO:HI], l_i_d[LO:HI][LO:HI];
    wire          n_i_v[LO:HI][LO:HI], e_i_v[LO:HI][LO:HI], s_i_v[LO:HI][LO:HI],
                  w_i_v[LO:HI][LO:HI], l_i_v[LO:HI][LO:HI];
    wire          n_i_b[LO:HI][LO:HI], e_i_b[LO:HI][LO:HI], s_i_b[LO:HI][LO:HI],
                  w_i_b[LO:HI][LO:HI], l_i_b[LO:HI][LO:HI];

    genvar gx, gy;
    generate
      for (gx = LO; gx <= HI; gx = gx + 1) begin : gx_
        for (gy = LO; gy <= HI; gy = gy + 1) begin : gy_
          NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("distributed"),
                      .POS_WIDTH(POSW), .POS_X(gx), .POS_Y(gy),
                      .GRID_LO(LO), .GRID_HI(HI)) r (
            .clk(clk), .rst(!resetn),
            .north_in_data(n_i_d[gx][gy]), .north_in_valid(n_i_v[gx][gy]), .north_in_busy(n_i_b[gx][gy]),
            .north_out_data(n_o_d[gx][gy]), .north_out_valid(n_o_v[gx][gy]), .north_out_busy(n_o_b[gx][gy]),
            .east_in_data(e_i_d[gx][gy]), .east_in_valid(e_i_v[gx][gy]), .east_in_busy(e_i_b[gx][gy]),
            .east_out_data(e_o_d[gx][gy]), .east_out_valid(e_o_v[gx][gy]), .east_out_busy(e_o_b[gx][gy]),
            .south_in_data(s_i_d[gx][gy]), .south_in_valid(s_i_v[gx][gy]), .south_in_busy(s_i_b[gx][gy]),
            .south_out_data(s_o_d[gx][gy]), .south_out_valid(s_o_v[gx][gy]), .south_out_busy(s_o_b[gx][gy]),
            .west_in_data(w_i_d[gx][gy]), .west_in_valid(w_i_v[gx][gy]), .west_in_busy(w_i_b[gx][gy]),
            .west_out_data(w_o_d[gx][gy]), .west_out_valid(w_o_v[gx][gy]), .west_out_busy(w_o_b[gx][gy]),
            .local_in_data(l_i_d[gx][gy]), .local_in_valid(l_i_v[gx][gy]), .local_in_busy(l_i_b[gx][gy]),
            .local_out_data(l_o_d[gx][gy]), .local_out_valid(l_o_v[gx][gy]), .local_out_busy(l_o_b[gx][gy])
          );

          if (gy > LO) begin : up
            assign s_i_d[gx][gy-1] = n_o_d[gx][gy];
            assign s_i_v[gx][gy-1] = n_o_v[gx][gy];
            assign n_o_b[gx][gy]   = s_i_b[gx][gy-1];
          end else begin : up_e
            assign n_o_b[gx][gy] = 1'b0;
            assign n_i_d[gx][gy] = {FW{1'b0}}; assign n_i_v[gx][gy] = 1'b0;
          end
          if (gy < HI) begin : dn
            assign n_i_d[gx][gy+1] = s_o_d[gx][gy];
            assign n_i_v[gx][gy+1] = s_o_v[gx][gy];
            assign s_o_b[gx][gy]   = n_i_b[gx][gy+1];
          end else begin : dn_e
            assign s_o_b[gx][gy] = 1'b0;
            assign s_i_d[gx][gy] = {FW{1'b0}}; assign s_i_v[gx][gy] = 1'b0;
          end
          if (gx < HI) begin : rt
            assign w_i_d[gx+1][gy] = e_o_d[gx][gy];
            assign w_i_v[gx+1][gy] = e_o_v[gx][gy];
            assign e_o_b[gx][gy]   = w_i_b[gx+1][gy];
          end else begin : rt_e
            assign e_o_b[gx][gy] = 1'b0;
            assign e_i_d[gx][gy] = {FW{1'b0}}; assign e_i_v[gx][gy] = 1'b0;
          end
          if (gx > LO) begin : lf
            assign e_i_d[gx-1][gy] = w_o_d[gx][gy];
            assign e_i_v[gx-1][gy] = w_o_v[gx][gy];
            assign w_o_b[gx][gy]   = e_i_b[gx-1][gy];
          end else begin : lf_e
            assign w_o_b[gx][gy] = 1'b0;
            assign w_i_d[gx][gy] = {FW{1'b0}}; assign w_i_v[gx][gy] = 1'b0;
          end
        end
      end
    endgenerate

    // orchestrator sits on the local port of router (LO,LO)
    assign l_i_d[LO][LO] = orc_out_data;
    assign l_i_v[LO][LO] = orc_out_valid;
    assign orc_out_busy  = l_i_b[LO][LO];
    assign orc_in_data   = l_o_d[LO][LO];
    assign orc_in_valid  = l_o_v[LO][LO];
    assign l_o_b[LO][LO] = orc_in_busy;

    // Loopback node at (HI,HI): swap src/dst and send it back. Turns "did the
    // flit route correctly" into something observable purely through RX.
    reg [FW-1:0] lb_data; reg lb_valid;
    assign l_i_d[HI][HI] = lb_data;
    assign l_i_v[HI][HI] = lb_valid;
    assign l_o_b[HI][HI] = 1'b0;
    always @(posedge clk) begin
        if (!resetn) begin lb_valid <= 1'b0; lb_data <= {FW{1'b0}}; end
        else if (l_o_v[HI][HI] && !l_i_b[HI][HI]) begin
            lb_data <= { l_o_d[HI][HI][FW-2*POSW-1 -: 2*POSW],   // old src -> dst
                         l_o_d[HI][HI][FW-1        -: 2*POSW],   // old dst -> src
                         l_o_d[HI][HI][FW-4*POSW-1 : 0] };
            lb_valid <= 1'b1;
        end
        else begin
            lb_valid <= 1'b0;
        end
    end

    // unused local ports
    assign l_i_d[HI][LO] = {FW{1'b0}}; assign l_i_v[HI][LO] = 1'b0;
    assign l_o_b[HI][LO] = 1'b0;
    assign l_i_d[LO][HI] = {FW{1'b0}}; assign l_i_v[LO][HI] = 1'b0;
    assign l_o_b[LO][HI] = 1'b0;

    // --------------------------------------------------------------- AXI tasks
    task axi_write1(input [31:0] a, input [DW-1:0] d);
        begin
            @(posedge clk);
            awaddr <= a; awlen <= 0; awid <= 4'h1; awvalid <= 1'b1;
            @(posedge clk); while (!awready) @(posedge clk);
            awvalid <= 1'b0;
            wdata <= d; wlast <= 1'b1; wvalid <= 1'b1; bready <= 1'b1;
            @(posedge clk); while (!wready) @(posedge clk);
            wvalid <= 1'b0; wlast <= 1'b0;
            @(posedge clk); while (!bvalid) @(posedge clk);
            bready <= 1'b0;
        end
    endtask

    task axi_read1(input [31:0] a, output [DW-1:0] d);
        begin
            @(posedge clk);
            araddr <= a; arlen <= 0; arid <= 4'h2; arvalid <= 1'b1;
            @(posedge clk); while (!arready) @(posedge clk);
            arvalid <= 1'b0; rready <= 1'b1;
            @(posedge clk); while (!rvalid) @(posedge clk);
            d = rdata;
            @(posedge clk); rready <= 1'b0;
        end
    endtask

    task expect_eq(input [DW-1:0] got, input [DW-1:0] want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                $display("[%0t] FAIL %0s: got %h want %h", $time, what, got, want);
                errors = errors + 1;
            end
        end
    endtask

    // ------------------------------------------------------------- stimulus
    reg [FW-1:0] flit, got_flit;
    reg [DW-1:0] rv;
    integer i, j, guard, n_back;

    initial begin
        $dumpfile("noc_orchestrator_tb.vcd");
        $dumpvars(0, noc_orchestrator_tb);
        resetn = 0; repeat (16) @(posedge clk);
        resetn = 1; repeat (8)  @(posedge clk);

        $display("--- CAPS discovery ---");
        axi_read1(A_CAPS, rv);
        expect_eq(rv[15:0],  FW,   "CAPS.flit_width");
        expect_eq(rv[23:16], POSW, "CAPS.pos_width");
        expect_eq(rv[31:24], LO,   "CAPS.grid_lo");
        expect_eq(rv[39:32], HI,   "CAPS.grid_hi");

        $display("--- RX empty before any traffic ---");
        axi_read1(A_RX_STATUS, rv);
        expect_eq(rv[16], 1'b1, "RX_STATUS.empty");

        $display("--- inject a flit to (HI,HI), expect it looped back ---");
        // dst=(HI,HI) src=(LO,LO) type=CU_DATA id=0x5A last=1.
        // Deliberately not CU_SIGNAL: signals are absorbed into NODE_STATUS and
        // never enter RX, so they cannot demonstrate a round trip. RX carries the
        // traffic that has no other home.
        flit = {HI[3:0], HI[3:0], LO[3:0], LO[3:0], 4'h4, 8'h5A, 1'b1, 3'b000,
                8'h03, 32'hCAFE_1234, 216'd0};
        for (i = 0; i < WORDS; i = i + 1) begin
            axi_write1(A_TX_FLIT0 + i*8, flit[i*DW +: DW]);
        end
        axi_write1(A_TX_KICK, 64'd1);

        guard = 0;
        axi_read1(A_RX_STATUS, rv);
        while (rv[16] && guard < 200) begin
            repeat (10) @(posedge clk);
            axi_read1(A_RX_STATUS, rv);
            guard = guard + 1;
        end
        if (rv[16]) begin
            $display("[%0t] FAIL: nothing arrived in RX", $time);
            errors = errors + 1;
        end else begin
            for (i = 0; i < WORDS; i = i + 1) begin
                axi_read1(A_RX_FLIT0 + i*8, rv);
                got_flit[i*DW +: DW] = rv;
            end
            // loopback swapped src/dst, payload preserved
            expect_eq(got_flit[FW-1 -: 4],        LO, "returned dst_x");
            expect_eq(got_flit[FW-5 -: 4],        LO, "returned dst_y");
            expect_eq(got_flit[FW-9 -: 4],        HI, "returned src_x");
            expect_eq(got_flit[FW-13 -: 4],       HI, "returned src_y");
            expect_eq(got_flit[247 -: 32], 32'hCAFE_1234, "payload survived");
            axi_write1(A_RX_POP, 64'd1);
        end

        $display("--- RX drained after pop ---");
        axi_read1(A_RX_STATUS, rv);
        expect_eq(rv[16], 1'b1, "RX_STATUS.empty after pop");

        $display("--- a CU_SIGNAL updates the status mirror, bypassing RX ---");
        // The loopback returns this with src=(HI,HI), which is the index the
        // mirror files it under.
        flit = {HI[3:0], HI[3:0], LO[3:0], LO[3:0], 4'h6, 8'h5A, 1'b1, 3'b000,
                8'h03, 32'hCAFE_1234, 216'd0};
        for (i = 0; i < WORDS; i = i + 1) begin
            axi_write1(A_TX_FLIT0 + i*8, flit[i*DW +: DW]);
        end
        axi_write1(A_TX_KICK, 64'd1);

        guard = 0;
        axi_read1(A_NODE + ({HI[3:0], HI[3:0]}) * 8, rv);
        while (!rv[0] && guard < 200) begin
            repeat (10) @(posedge clk);
            axi_read1(A_NODE + ({HI[3:0], HI[3:0]}) * 8, rv);
            guard = guard + 1;
        end
        expect_eq(rv[0],      1'b1,          "NODE_STATUS.valid");
        expect_eq(rv[63:56],  8'h03,         "NODE_STATUS.last_code");
        expect_eq(rv[55:24],  32'hCAFE_1234, "NODE_STATUS.last_arg");
        expect_eq(rv[23:8],   16'd1,         "NODE_STATUS.signal_count");

        // the signal must NOT have consumed an RX slot
        axi_read1(A_RX_STATUS, rv);
        expect_eq(rv[16], 1'b1, "RX still empty after a signal");

        $display("--- instruction dispatch from the staging buffer ---");
        // Stage 3 CU_INST flits. dst is a placeholder: the dispatcher rewrites it
        // to PROG_DST, which is the whole point -- one staged program, any target.
        for (i = 0; i < 3; i = i + 1) begin
            flit = {8'h00, LO[3:0], LO[3:0], 4'h5, 8'h00 + i[7:0], 1'b1, 3'b000,
                    8'h00, 8'h00, 240'hAA00 + i};
            for (j = 0; j < WORDS; j = j + 1) begin
                axi_write1(32'h2000 + (i*WORDS + j)*8, flit[j*DW +: DW]);
            end
        end
        axi_write1(A_PROG_DST,  {HI[3:0], HI[3:0]});   // {dst_y, dst_x} = (HI,HI)
        axi_write1(A_PROG_LEN,  64'd3);
        axi_write1(A_PROG_CRED, 64'd8);
        axi_write1(A_PROG_KICK, 64'd1);

        // the loopback node returns each one, so all three should come back
        guard = 0; n_back = 0;
        while (n_back < 3 && guard < 400) begin
            axi_read1(A_RX_STATUS, rv);
            if (!rv[16]) begin
                for (j = 0; j < WORDS; j = j + 1) begin
                    axi_read1(A_RX_FLIT0 + j*8, rv);
                    got_flit[j*DW +: DW] = rv;
                end
                // loopback swapped, so src is where the dispatcher sent it
                expect_eq(got_flit[FW-9  -: 4], HI, "dispatched to PROG_DST x");
                expect_eq(got_flit[FW-13 -: 4], HI, "dispatched to PROG_DST y");
                expect_eq(got_flit[FW-4*POSW-1 -: 4], 4'h5, "type stayed CU_INST");
                axi_write1(A_RX_POP, 64'd1);
                n_back = n_back + 1;
            end else begin
                repeat (10) @(posedge clk);
            end
            guard = guard + 1;
        end
        expect_eq(n_back[15:0], 16'd3, "all 3 dispatched flits returned");

        axi_read1(A_PROG_STAT, rv);
        expect_eq(rv[0], 1'b0, "PROG_STATUS.running clear when done");

        repeat (20) @(posedge clk);
        $display("");
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
        #500000;
        $display("========================================");
        $display("  FAIL -- WATCHDOG TIMEOUT (AXI or NoC handshake stuck)");
        $display("========================================");
        $finish;
    end

endmodule
