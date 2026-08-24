// Multi-CU system test on a 3x3 mesh.
//
//   (1,1) orchestrator
//   (3,3) cu_alu    -- consumes instructions, computes, reports results
//   (1,3) cu_relay A-- originates CU_DATA traffic to a peer
//   (3,1) cu_relay B-- ditto, and receives A's
//
// Two CU types on one framework, so a framework accidentally fitted to a single
// CU style would fail here. Covers:
//
//   discovery       CU_CTRL read of CAPS, distinguishing the two types
//   compute         a program of real opcodes, checked against a model
//   fault           opcode 0xFF -> SIG_FAULT rather than BATCH_COMPLETE
//   CU<->CU         relay A instructed to send CU_DATA to relay B
//   concurrency     programs dispatched to different CUs, all completing
//   backpressure    a program longer than the instruction FIFO

`timescale 1ns/1ps

module noc_multicu_tb;

    localparam DW = 64, FW = 288, POSW = 4;
    localparam LO = 1, HI = 3;              // 3x3
    localparam WORDS = 5;
    localparam ORC_X = 1, ORC_Y = 1;
    localparam ALU_X = 3, ALU_Y = 3;
    localparam RA_X  = 1, RA_Y  = 3;
    localparam RB_X  = 3, RB_Y  = 1;

    localparam A_CAPS = 16'h0010, A_PROG_DST = 16'h0040, A_PROG_LEN = 16'h0048;
    localparam A_PROG_KICK = 16'h0050, A_PROG_STAT = 16'h0058;
    localparam A_PROG_CRED = 16'h0060, A_TX_FLIT0 = 16'h0100;
    localparam A_TX_KICK = 16'h0140, A_RX_FLIT0 = 16'h0180, A_RX_POP = 16'h01C0;
    localparam A_RX_STATUS = 16'h01C8, A_NODE = 16'h1000, A_STAGE = 16'h2000;
    localparam SIG_BATCH = 8'h01, SIG_FAULT = 8'h04;
    localparam T_CU_CTRL = 4'h7;

    reg clk = 0, resetn = 0;
    always begin
        #5 clk = ~clk;
    end
    integer errors = 0, checks = 0;

    reg  [3:0]  awid=0, arid=0;
    reg  [31:0] awaddr=0, araddr=0;
    reg         awvalid=0, arvalid=0, wvalid=0, wlast=0, bready=0, rready=0;
    reg  [DW-1:0] wdata=0;
    wire awready, wready, arready, bvalid, rvalid, rlast;
    wire [3:0] bid, rid;
    wire [DW-1:0] rdata;

    wire [FW-1:0] orc_out_d, orc_in_d;
    wire orc_out_v, orc_in_v, orc_out_b, orc_in_b;

    noc_orchestrator #(
        .DATA_WIDTH(DW), .ADDR_WIDTH(32), .ID_WIDTH(4), .FLIT_WIDTH(FW),
        .POS_WIDTH(POSW), .GRID_LO(LO), .GRID_HI(HI), .ORC_X(ORC_X), .ORC_Y(ORC_Y),
        .STAGE_FLITS(256)
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

    assign l_i_d[ORC_X][ORC_Y]=orc_out_d; assign l_i_v[ORC_X][ORC_Y]=orc_out_v;
    assign orc_out_b=l_i_b[ORC_X][ORC_Y];
    assign orc_in_d=l_o_d[ORC_X][ORC_Y];  assign orc_in_v=l_o_v[ORC_X][ORC_Y];
    assign l_o_b[ORC_X][ORC_Y]=orc_in_b;

    // ------------------------------------------------------------------ CUs
    wire [FW-1:0] alu_o, ra_o, rb_o;
    wire alu_ov, ra_ov, rb_ov, alu_ib, ra_ib, rb_ib;
    wire [31:0] alu_retired, alu_faults, alu_acc;
    wire [31:0] ra_sent, ra_recv, rb_sent, rb_recv;
    wire [63:0] ra_xor, rb_xor;

    cu_alu #(.FLIT_WIDTH(FW), .POS_WIDTH(POSW), .POS_X(ALU_X), .POS_Y(ALU_Y)) alu (
        .clk(clk), .resetn(resetn),
        .noc_in_data(l_o_d[ALU_X][ALU_Y]), .noc_in_valid(l_o_v[ALU_X][ALU_Y]),
        .noc_in_busy(alu_ib),
        .noc_out_data(alu_o), .noc_out_valid(alu_ov), .noc_out_busy(l_i_b[ALU_X][ALU_Y]),
        .retired(alu_retired), .faults(alu_faults), .acc(alu_acc));
    assign l_i_d[ALU_X][ALU_Y]=alu_o; assign l_i_v[ALU_X][ALU_Y]=alu_ov;
    assign l_o_b[ALU_X][ALU_Y]=alu_ib;

    cu_relay #(.FLIT_WIDTH(FW), .POS_WIDTH(POSW), .POS_X(RA_X), .POS_Y(RA_Y)) rela (
        .clk(clk), .resetn(resetn),
        .noc_in_data(l_o_d[RA_X][RA_Y]), .noc_in_valid(l_o_v[RA_X][RA_Y]),
        .noc_in_busy(ra_ib),
        .noc_out_data(ra_o), .noc_out_valid(ra_ov), .noc_out_busy(l_i_b[RA_X][RA_Y]),
        .sent_count(ra_sent), .recv_count(ra_recv), .recv_xor(ra_xor));
    assign l_i_d[RA_X][RA_Y]=ra_o; assign l_i_v[RA_X][RA_Y]=ra_ov;
    assign l_o_b[RA_X][RA_Y]=ra_ib;

    cu_relay #(.FLIT_WIDTH(FW), .POS_WIDTH(POSW), .POS_X(RB_X), .POS_Y(RB_Y)) relb (
        .clk(clk), .resetn(resetn),
        .noc_in_data(l_o_d[RB_X][RB_Y]), .noc_in_valid(l_o_v[RB_X][RB_Y]),
        .noc_in_busy(rb_ib),
        .noc_out_data(rb_o), .noc_out_valid(rb_ov), .noc_out_busy(l_i_b[RB_X][RB_Y]),
        .sent_count(rb_sent), .recv_count(rb_recv), .recv_xor(rb_xor));
    assign l_i_d[RB_X][RB_Y]=rb_o; assign l_i_v[RB_X][RB_Y]=rb_ov;
    assign l_o_b[RB_X][RB_Y]=rb_ib;

    // idle local ports
    genvar ix, iy;
    generate
      for (ix = LO; ix <= HI; ix = ix + 1) begin : ip_
        for (iy = LO; iy <= HI; iy = iy + 1) begin : iq_
          if (!(
              (ix==ORC_X&&iy==ORC_Y)
              || (ix==ALU_X&&iy==ALU_Y)
              || (ix==RA_X&&iy==RA_Y)
              || (ix==RB_X&&iy==RB_Y)
          )) begin : idle
            assign l_i_d[ix][iy]={FW{1'b0}}; assign l_i_v[ix][iy]=1'b0;
            assign l_o_b[ix][iy]=1'b0;
          end
        end
      end
    endgenerate

    // --------------------------------------------------------------- tasks
    task axi_w(input [31:0] a, input [DW-1:0] d);
        begin
            @(posedge clk); awaddr<=a; awid<=4'h1; awvalid<=1'b1;
            @(posedge clk); while(!awready) @(posedge clk); awvalid<=1'b0;
            wdata<=d; wlast<=1'b1; wvalid<=1'b1; bready<=1'b1;
            @(posedge clk); while(!wready) @(posedge clk); wvalid<=1'b0; wlast<=1'b0;
            @(posedge clk); while(!bvalid) @(posedge clk); bready<=1'b0;
        end
    endtask
    task axi_r(input [31:0] a, output [DW-1:0] d);
        begin
            @(posedge clk); araddr<=a; arid<=4'h2; arvalid<=1'b1;
            @(posedge clk); while(!arready) @(posedge clk); arvalid<=1'b0; rready<=1'b1;
            @(posedge clk); while(!rvalid) @(posedge clk); d=rdata;
            @(posedge clk); rready<=1'b0;
        end
    endtask
    task chk(input [63:0] g, input [63:0] w, input [255:0] what);
        begin
            checks = checks + 1;
            if (g !== w) begin
                $display("  FAIL %0s: got %0h want %0h", what, g, w);
                errors = errors + 1;
            end
        end
    endtask

    reg [FW-1:0] f, got;
    reg [DW-1:0] rv;
    integer i, j, guard;

    // dispatch n staged flits to (dx,dy) with program id pid
    task dispatch(input [7:0] pid, input [3:0] dx, input [3:0] dy, input integer n);
        begin
            axi_w(A_PROG_DST, {dy, dx});
            axi_w(A_PROG_LEN, n);
            axi_w(A_PROG_CRED, 64'd24);
            axi_w(A_PROG_KICK, 64'd1);
        end
    endtask

    task wait_batch(input [7:0] pid, input [3:0] dx, input [3:0] dy, input [7:0] code);
        begin
            guard = 0;
            axi_r(A_NODE + ({dy, dx}) * 8, rv);
            while (!(rv[0] && rv[63:56]==code && rv[31:24]==pid) && guard < 800) begin
                repeat (20) @(posedge clk);
                axi_r(A_NODE + ({dy, dx}) * 8, rv);
                guard = guard + 1;
            end
            chk(rv[63:56], code, "signal code");
            // only BATCH_COMPLETE carries the program id; a FAULT carries the
            // datapath's exec_result instead
            if (code == SIG_BATCH) begin
                chk(rv[31:24], pid, "program id");
            end
        end
    endtask

    // stage an ALU program: n ops, opcode i%7, a = 100+i, b = i+1
    reg [31:0] model_acc; reg [31:0] mi;
    reg [7:0]  op8; reg [31:0] a32; reg [23:0] b24;   // explicit widths: an
    // unsized expression contributes 32 bits to a concatenation, which silently
    // pushed the flit past 288 and shifted every header field.
    task stage_alu(input [7:0] pid, input integer n, input integer fault_at);
        begin
            model_acc = 32'd0;
            for (i = 0; i < n; i = i + 1) begin
                op8 = i % 7; a32 = 100 + i; b24 = 1 + i;
                if (i == fault_at) begin
                    f = {16'h0000, 4'h5, pid, (i==n-1), 3'b000, 192'd0,
                         8'hFF, 32'd0, 24'd0};
                end
                else begin
                    f = {16'h0000, 4'h5, pid, (i==n-1), 3'b000, 192'd0,
                         op8, a32, b24};
                    case (op8)
                        0: mi = a32 + {8'd0, b24};   1: mi = a32 - {8'd0, b24};
                        2: mi = a32 & {8'd0, b24};   3: mi = a32 | {8'd0, b24};
                        4: mi = a32 ^ {8'd0, b24};   5: mi = a32 << b24[4:0];
                        default: mi = a32 * {8'd0, b24};
                    endcase
                    model_acc = model_acc + mi;
                end
                for (j = 0; j < WORDS; j = j + 1) begin
                    axi_w(A_STAGE + (i*WORDS + j)*8, f[j*DW +: DW]);
                end
            end
        end
    endtask

    initial begin
        $dumpfile("noc_multicu_tb.vcd");
        $dumpvars(0, noc_multicu_tb);
        resetn=0; repeat(20) @(posedge clk); resetn=1; repeat(10) @(posedge clk);

        // ---------------------------------------------------------------
        $display("--- 1. CU discovery over CU_CTRL ---");
        // read CAPS (reg 0) from the ALU via the raw mailbox
        f = {ALU_X[3:0], ALU_Y[3:0], ORC_X[3:0], ORC_Y[3:0], T_CU_CTRL, 8'h77,
             1'b1, 3'b000, 8'd0 /*read*/, 8'd0 /*CAPS*/, 240'd0};
        for (j=0;j<WORDS;j=j+1) begin
            axi_w(A_TX_FLIT0 + j*8, f[j*DW +: DW]);
        end
        axi_w(A_TX_KICK, 64'd1);
        guard=0; axi_r(A_RX_STATUS, rv);
        while (rv[16] && guard<200) begin repeat(10) @(posedge clk); axi_r(A_RX_STATUS,rv); guard=guard+1; end
        chk(rv[16], 1'b0, "CU_CTRL reply arrived");
        for (j=0;j<WORDS;j=j+1) begin axi_r(A_RX_FLIT0 + j*8, rv); got[j*DW +: DW]=rv; end
        chk(got[239 -: 16], 16'h0A10, "ALU reports CU_TYPE 0x0A10");
        axi_w(A_RX_POP, 64'd1);

        f = {RA_X[3:0], RA_Y[3:0], ORC_X[3:0], ORC_Y[3:0], T_CU_CTRL, 8'h78,
             1'b1, 3'b000, 8'd0, 8'd0, 240'd0};
        for (j=0;j<WORDS;j=j+1) begin
            axi_w(A_TX_FLIT0 + j*8, f[j*DW +: DW]);
        end
        axi_w(A_TX_KICK, 64'd1);
        guard=0; axi_r(A_RX_STATUS, rv);
        while (rv[16] && guard<200) begin repeat(10) @(posedge clk); axi_r(A_RX_STATUS,rv); guard=guard+1; end
        for (j=0;j<WORDS;j=j+1) begin axi_r(A_RX_FLIT0 + j*8, rv); got[j*DW +: DW]=rv; end
        chk(got[239 -: 16], 16'h0E11, "relay reports CU_TYPE 0x0E11 (different)");
        axi_w(A_RX_POP, 64'd1);

        // ---------------------------------------------------------------
        $display("--- 2. ALU program, results checked against a model ---");
        stage_alu(8'h31, 12, -1);
        dispatch(8'h31, ALU_X, ALU_Y, 12);
        wait_batch(8'h31, ALU_X, ALU_Y, SIG_BATCH);
        chk(alu_retired, 32'd12,   "ALU retired 12 instructions");
        chk(alu_faults,  32'd0,    "no faults");
        chk(alu_acc,     model_acc,"ALU accumulator matches the model");
        $display("    12 ops, acc = %0h (model %0h)", alu_acc, model_acc);

        // ---------------------------------------------------------------
        $display("--- 3. fault instruction reports SIG_FAULT ---");
        stage_alu(8'h32, 3, 2);          // last instruction faults
        dispatch(8'h32, ALU_X, ALU_Y, 3);
        wait_batch(8'h32, ALU_X, ALU_Y, SIG_FAULT);
        chk(alu_faults, 32'd1, "one fault recorded");

        // ---------------------------------------------------------------
        $display("--- 4. CU -> CU data transfer ---");
        for (i = 0; i < 5; i = i + 1) begin
            f = {16'h0000, 4'h5, 8'h41, (i==4), 3'b000, 192'd0,
                 RB_X[7:0], RB_Y[7:0], (32'hBEEF0000 + i), 16'h0102};
            for (j=0;j<WORDS;j=j+1) begin
                axi_w(A_STAGE + (i*WORDS+j)*8, f[j*DW +: DW]);
            end
        end
        dispatch(8'h41, RA_X, RA_Y, 5);
        wait_batch(8'h41, RA_X, RA_Y, SIG_BATCH);
        repeat (200) @(posedge clk);
        chk(ra_sent, 32'd5, "relay A sent 5 packets");
        chk(rb_recv, 32'd5, "relay B received 5 packets");
        $display("    A sent %0d, B received %0d", ra_sent, rb_recv);

        // ---------------------------------------------------------------
        $display("--- 5. concurrent programs to different CUs ---");
        stage_alu(8'h51, 8, -1);
        dispatch(8'h51, ALU_X, ALU_Y, 8);
        // The dispatcher streams out of the staging buffer, so it must finish
        // reading before the buffer is refilled -- wait for PROG_STATUS.running
        // to clear, NOT for the CU to finish executing. The ALU keeps working
        // while relay B's program is staged and dispatched; that overlap is the
        // concurrency being tested.
        guard=0; axi_r(A_PROG_STAT, rv);
        while (rv[0] && guard<400) begin repeat(10) @(posedge clk); axi_r(A_PROG_STAT,rv); guard=guard+1; end
        for (i = 0; i < 4; i = i + 1) begin
            f = {16'h0000, 4'h5, 8'h52, (i==3), 3'b000, 192'd0,
                 RA_X[7:0], RA_Y[7:0], (32'hFEED0000 + i), 16'h0304};
            for (j=0;j<WORDS;j=j+1) begin
                axi_w(A_STAGE + (i*WORDS+j)*8, f[j*DW +: DW]);
            end
        end
        dispatch(8'h52, RB_X, RB_Y, 4);
        wait_batch(8'h51, ALU_X, ALU_Y, SIG_BATCH);
        wait_batch(8'h52, RB_X,  RB_Y,  SIG_BATCH);
        repeat (200) @(posedge clk);
        chk(alu_retired, 32'd22, "ALU total 12+2+8 retired");   // 12 + 2 ok + 8
        chk(rb_sent,     32'd4,  "relay B sent 4");
        chk(ra_recv,     32'd4,  "relay A received 4");

        // ---------------------------------------------------------------
        $display("--- 6. program longer than the instruction FIFO ---");
        stage_alu(8'h61, 100, -1);
        dispatch(8'h61, ALU_X, ALU_Y, 100);
        wait_batch(8'h61, ALU_X, ALU_Y, SIG_BATCH);
        chk(alu_retired, 32'd122, "ALU retired all 100 more");
        $display("    100-instruction program through a 32-deep FIFO");

        repeat (50) @(posedge clk);
        $display("");
        $display("========================================");
        if (errors==0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #40_000_000;
        $display("========================================");
        $display("  FAIL -- WATCHDOG TIMEOUT");
        $display("  alu_retired=%0d faults=%0d ra_sent=%0d rb_recv=%0d",
                 alu_retired, alu_faults, ra_sent, rb_recv);
        $display("========================================");
        $finish;
    end

endmodule
