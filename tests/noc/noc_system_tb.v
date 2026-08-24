// Full-system test: AXI4 -> orchestrator -> NoC -> pseudo CU -> status -> AXI4.
//
//   host stages a program over AXI                     (STAGE[])
//   host names a target and kicks                      (PROG_DST / PROG_LEN / PROG_KICK)
//   orchestrator dispatches CU_INST across the mesh
//   CU executes, emits INST_COMPLETE per instruction
//   CU emits BATCH_COMPLETE carrying the program id on the flit marked `last`
//   host polls NODE_STATUS[cu] until that program id shows up
//
// The point of the last step: in production a CU writes its results straight to
// DRAM and the orchestrator never sees them, so completion is all the host can
// learn from this path. This models that, rather than routing results back.
//
// Two programs run in sequence with different ids, because "is program X done"
// is only a meaningful question if polling can tell them apart.

`timescale 1ns/1ps

module noc_system_tb;

    localparam DW = 64, FW = 288, POSW = 4;
    localparam LO = 1, HI = 2;
    localparam WORDS = 5;
    localparam ORC_X = LO, ORC_Y = LO;      // orchestrator at (1,1)
    localparam CU_X  = HI, CU_Y  = HI;      // pseudo CU at (2,2)

    localparam A_CAPS = 16'h0010, A_PROG_DST = 16'h0040, A_PROG_LEN = 16'h0048;
    localparam A_PROG_KICK = 16'h0050, A_PROG_STAT = 16'h0058;
    localparam A_PROG_CRED = 16'h0060, A_RX_STATUS = 16'h01C8;
    localparam A_RX_POP = 16'h01C0, A_NODE = 16'h1000, A_STAGE = 16'h2000;
    localparam SIG_BATCH_COMPLETE = 8'h01;

    reg clk = 0, resetn = 0;
    always begin
        #5 clk = ~clk;
    end
    integer errors = 0, checks = 0;

    // ---------------------------------------------------------------- AXI
    reg  [3:0]  awid=0, arid=0;
    reg  [31:0] awaddr=0, araddr=0;
    reg         awvalid=0, arvalid=0, wvalid=0, wlast=0, bready=0, rready=0;
    reg  [DW-1:0] wdata=0;
    wire awready, wready, arready, bvalid, rvalid, rlast;
    wire [3:0] bid, rid;
    wire [DW-1:0] rdata;

    wire [FW-1:0] orc_out_d, orc_in_d;
    wire          orc_out_v, orc_in_v, orc_out_b, orc_in_b;

    noc_orchestrator #(
        .DATA_WIDTH(DW), .ADDR_WIDTH(32), .ID_WIDTH(4), .FLIT_WIDTH(FW),
        .POS_WIDTH(POSW), .GRID_LO(LO), .GRID_HI(HI),
        .ORC_X(ORC_X), .ORC_Y(ORC_Y)
    ) orc (
        .clk(clk), .resetn(resetn),
        .s_axi_awid(awid), .s_axi_awaddr(awaddr), .s_axi_awlen(8'd0),
        .s_axi_awsize(3'd3), .s_axi_awburst(2'b01),
        .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb({(DW/8){1'b1}}), .s_axi_wlast(wlast),
        .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bid(bid), .s_axi_bresp(), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_arid(arid), .s_axi_araddr(araddr), .s_axi_arlen(8'd0),
        .s_axi_arsize(3'd3), .s_axi_arburst(2'b01),
        .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rid(rid), .s_axi_rdata(rdata), .s_axi_rresp(),
        .s_axi_rlast(rlast), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .noc_out_data(orc_out_d), .noc_out_valid(orc_out_v), .noc_out_busy(orc_out_b),
        .noc_in_data(orc_in_d), .noc_in_valid(orc_in_v), .noc_in_busy(orc_in_b)
    );

    // ---------------------------------------------------------------- mesh
    wire [FW-1:0] n_o_d[LO:HI][LO:HI], e_o_d[LO:HI][LO:HI], s_o_d[LO:HI][LO:HI],
                  w_o_d[LO:HI][LO:HI], l_o_d[LO:HI][LO:HI];
    wire n_o_v[LO:HI][LO:HI], e_o_v[LO:HI][LO:HI], s_o_v[LO:HI][LO:HI],
         w_o_v[LO:HI][LO:HI], l_o_v[LO:HI][LO:HI];
    wire n_o_b[LO:HI][LO:HI], e_o_b[LO:HI][LO:HI], s_o_b[LO:HI][LO:HI],
         w_o_b[LO:HI][LO:HI], l_o_b[LO:HI][LO:HI];
    wire [FW-1:0] n_i_d[LO:HI][LO:HI], e_i_d[LO:HI][LO:HI], s_i_d[LO:HI][LO:HI],
                  w_i_d[LO:HI][LO:HI], l_i_d[LO:HI][LO:HI];
    wire n_i_v[LO:HI][LO:HI], e_i_v[LO:HI][LO:HI], s_i_v[LO:HI][LO:HI],
         w_i_v[LO:HI][LO:HI], l_i_v[LO:HI][LO:HI];
    wire n_i_b[LO:HI][LO:HI], e_i_b[LO:HI][LO:HI], s_i_b[LO:HI][LO:HI],
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
            assign s_i_d[gx][gy-1]=n_o_d[gx][gy]; assign s_i_v[gx][gy-1]=n_o_v[gx][gy];
            assign n_o_b[gx][gy]=s_i_b[gx][gy-1];
          end else begin : upe
            assign n_o_b[gx][gy]=1'b0;
            assign n_i_d[gx][gy]={FW{1'b0}}; assign n_i_v[gx][gy]=1'b0;
          end
          if (gy < HI) begin : dn
            assign n_i_d[gx][gy+1]=s_o_d[gx][gy]; assign n_i_v[gx][gy+1]=s_o_v[gx][gy];
            assign s_o_b[gx][gy]=n_i_b[gx][gy+1];
          end else begin : dne
            assign s_o_b[gx][gy]=1'b0;
            assign s_i_d[gx][gy]={FW{1'b0}}; assign s_i_v[gx][gy]=1'b0;
          end
          if (gx < HI) begin : rt
            assign w_i_d[gx+1][gy]=e_o_d[gx][gy]; assign w_i_v[gx+1][gy]=e_o_v[gx][gy];
            assign e_o_b[gx][gy]=w_i_b[gx+1][gy];
          end else begin : rte
            assign e_o_b[gx][gy]=1'b0;
            assign e_i_d[gx][gy]={FW{1'b0}}; assign e_i_v[gx][gy]=1'b0;
          end
          if (gx > LO) begin : lf
            assign e_i_d[gx-1][gy]=w_o_d[gx][gy]; assign e_i_v[gx-1][gy]=w_o_v[gx][gy];
            assign w_o_b[gx][gy]=e_i_b[gx-1][gy];
          end else begin : lfe
            assign w_o_b[gx][gy]=1'b0;
            assign w_i_d[gx][gy]={FW{1'b0}}; assign w_i_v[gx][gy]=1'b0;
          end
        end
      end
    endgenerate

    assign l_i_d[ORC_X][ORC_Y] = orc_out_d;
    assign l_i_v[ORC_X][ORC_Y] = orc_out_v;
    assign orc_out_b           = l_i_b[ORC_X][ORC_Y];
    assign orc_in_d            = l_o_d[ORC_X][ORC_Y];
    assign orc_in_v            = l_o_v[ORC_X][ORC_Y];
    assign l_o_b[ORC_X][ORC_Y] = orc_in_b;

    wire [FW-1:0] cu_out_d;
    wire cu_out_v, cu_in_b;
    wire [31:0] cu_inst_count, cu_bad_count;
    wire [63:0] cu_body_xor;

    noc_pseudo_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(POSW), .POS_X(CU_X), .POS_Y(CU_Y),
                    .EXEC_CYCLES(4)) cu (
        .clk(clk), .resetn(resetn),
        .noc_in_data(l_o_d[CU_X][CU_Y]), .noc_in_valid(l_o_v[CU_X][CU_Y]),
        .noc_in_busy(cu_in_b),
        .noc_out_data(cu_out_d), .noc_out_valid(cu_out_v),
        .noc_out_busy(l_i_b[CU_X][CU_Y]),
        .inst_count(cu_inst_count), .bad_count(cu_bad_count), .body_xor(cu_body_xor)
    );
    assign l_i_d[CU_X][CU_Y] = cu_out_d;
    assign l_i_v[CU_X][CU_Y] = cu_out_v;
    assign l_o_b[CU_X][CU_Y] = cu_in_b;

    // idle local ports
    assign l_i_d[HI][LO]={FW{1'b0}}; assign l_i_v[HI][LO]=1'b0; assign l_o_b[HI][LO]=1'b0;
    assign l_i_d[LO][HI]={FW{1'b0}}; assign l_i_v[LO][HI]=1'b0; assign l_o_b[LO][HI]=1'b0;

    // --------------------------------------------------------------- tasks
    task axi_w(input [31:0] a, input [DW-1:0] d);
        begin
            @(posedge clk);
            awaddr <= a; awid <= 4'h1; awvalid <= 1'b1;
            @(posedge clk); while (!awready) @(posedge clk);
            awvalid <= 1'b0;
            wdata <= d; wlast <= 1'b1; wvalid <= 1'b1; bready <= 1'b1;
            @(posedge clk); while (!wready) @(posedge clk);
            wvalid <= 1'b0; wlast <= 1'b0;
            @(posedge clk); while (!bvalid) @(posedge clk);
            bready <= 1'b0;
        end
    endtask

    task axi_r(input [31:0] a, output [DW-1:0] d);
        begin
            @(posedge clk);
            araddr <= a; arid <= 4'h2; arvalid <= 1'b1;
            @(posedge clk); while (!arready) @(posedge clk);
            arvalid <= 1'b0; rready <= 1'b1;
            @(posedge clk); while (!rvalid) @(posedge clk);
            d = rdata;
            @(posedge clk); rready <= 1'b0;
        end
    endtask

    task chk(input [63:0] got, input [63:0] want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                $display("  FAIL %0s: got %0h want %0h", what, got, want);
                errors = errors + 1;
            end
        end
    endtask

    reg [FW-1:0] f;
    reg [DW-1:0] rv;
    reg [63:0]   want_xor, running_xor;
    integer i, j, guard, n_inst;

    // Stage a program of n_inst CU_INST flits with the given program id, dispatch
    // it to the CU, and poll NODE_STATUS until that id reports BATCH_COMPLETE.
    task run_program(input [7:0] pid, input integer n, input integer base_prev);
        begin
            want_xor = 64'd0;
            for (i = 0; i < n; i = i + 1) begin
                // dst/src are overwritten by the dispatcher; txn_id is the program id.
                // 32-bit header + 256-bit payload = 288; the body sits in the low 64.
                f = {8'h00, 8'h00, 4'h5, pid, (i == n-1), 3'b000,
                     192'd0, 64'hA5A5_0000_0000_0000 + (pid << 16) + i};
                want_xor = want_xor ^ (64'hA5A5_0000_0000_0000 + (pid << 16) + i);
                for (j = 0; j < WORDS; j = j + 1) begin
                    axi_w(32'h2000 + (i*WORDS + j)*8, f[j*DW +: DW]);
                end
            end
            axi_w(A_PROG_DST,  {CU_Y[3:0], CU_X[3:0]});
            axi_w(A_PROG_LEN,  n);
            axi_w(A_PROG_CRED, 64'd16);
            axi_w(A_PROG_KICK, 64'd1);

            // poll status, exactly as the host would
            guard = 0;
            axi_r(A_NODE + ({CU_Y[3:0], CU_X[3:0]}) * 8, rv);
            while (!(rv[0] && rv[63:56] == SIG_BATCH_COMPLETE && rv[31:24] == pid)
                   && guard < 500) begin
                repeat (20) @(posedge clk);
                axi_r(A_NODE + ({CU_Y[3:0], CU_X[3:0]}) * 8, rv);
                guard = guard + 1;
            end
            chk(rv[0],         1'b1,               "NODE_STATUS.valid");
            chk(rv[63:56],     SIG_BATCH_COMPLETE, "BATCH_COMPLETE reported");
            chk(rv[31:24],     pid,                "program id in last_arg");
            chk(cu_inst_count, base_prev + n,      "CU executed every instruction");
            chk(cu_bad_count,  32'd0,              "CU saw no malformed flit");
            chk(cu_body_xor,   running_xor ^ want_xor, "instruction bodies arrived intact");
            running_xor = running_xor ^ want_xor;
            $display("  program 0x%0h: %0d instructions, done after %0d polls", pid, n, guard);
        end
    endtask

    initial begin
        $dumpfile("noc_system_tb.vcd");
        $dumpvars(0, noc_system_tb);
        running_xor = 64'd0;
        resetn = 0; repeat (16) @(posedge clk);
        resetn = 1; repeat (8)  @(posedge clk);

        $display("--- discovery ---");
        axi_r(A_CAPS, rv);
        chk(rv[15:0], FW, "CAPS.flit_width");

        $display("--- program A ---");
        run_program(8'h11, 4, 0);

        $display("--- program B, different id ---");
        run_program(8'h22, 6, 4);

        $display("--- polling distinguishes the two programs ---");
        axi_r(A_NODE + ({CU_Y[3:0], CU_X[3:0]}) * 8, rv);
        chk(rv[31:24], 8'h22, "latest program id is B, not A");
        // one BATCH_COMPLETE per program plus one INST_COMPLETE per other instruction
        chk(rv[23:8], 16'd10, "signal_count == total instructions");

        $display("--- dispatcher idle ---");
        axi_r(A_PROG_STAT, rv);
        chk(rv[0], 1'b0, "PROG_STATUS.running clear");

        repeat (40) @(posedge clk);
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
        #3_000_000;
        $display("========================================");
        $display("  FAIL -- WATCHDOG TIMEOUT");
        $display("  inst_count=%0d bad=%0d", cu_inst_count, cu_bad_count);
        $display("========================================");
        $finish;
    end

endmodule
