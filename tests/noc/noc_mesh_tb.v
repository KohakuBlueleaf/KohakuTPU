// Self-checking mesh testbench for HakuNoC.
//
// Builds an N x N mesh, hangs an injector + receiver off every router's local
// port, and checks the four properties the spec claims in s10:
//
//   lossless      every injected flit arrives exactly once
//   correct       it arrives at the node named in dst_x/dst_y
//   in-order      per (source, destination) pair, from XY routing
//   no deadlock   the run completes; the watchdog firing IS the failure
//
// Unconnected edge ports are monitored, not tied off: a flit emitted there is a
// routing error, and silently dropping it would hide exactly the bug this bench
// exists to catch.
//
// Run: tests/run_noc_sim.ps1

`timescale 1ns/1ps

// mesh size: override at compile time with  xvlog -d MESH_N=3
`ifndef MESH_N
  `define MESH_N 2
`endif

module noc_mesh_tb;

    localparam DW    = 288;
    localparam POSW  = 4;
    localparam N     = `MESH_N;    // mesh is N x N
    localparam LO    = 1;
    localparam HI    = LO + N - 1;
    localparam NODES = N * N;

    localparam SEQ_LSB = 0;        // payload[15:0]  per (src,dst) sequence
    localparam SRC_LSB = 16;       // payload[23:16] flat source index

    reg clk = 0, rst = 1;
    always #5 clk = ~clk;

    integer errors = 0;
    integer sent_total = 0, recv_total = 0;

    // ---------------------------------------------------------------- links
    // [x][y], indexed LO..HI. *_o_* is driven by the router at [x][y].
    wire [DW-1:0] n_o_data [LO:HI][LO:HI], e_o_data [LO:HI][LO:HI],
                  s_o_data [LO:HI][LO:HI], w_o_data [LO:HI][LO:HI],
                  l_o_data [LO:HI][LO:HI];
    wire          n_o_val  [LO:HI][LO:HI], e_o_val  [LO:HI][LO:HI],
                  s_o_val  [LO:HI][LO:HI], w_o_val  [LO:HI][LO:HI],
                  l_o_val  [LO:HI][LO:HI];
    wire          n_o_busy [LO:HI][LO:HI], e_o_busy [LO:HI][LO:HI],
                  s_o_busy [LO:HI][LO:HI], w_o_busy [LO:HI][LO:HI],
                  l_o_busy [LO:HI][LO:HI];

    wire [DW-1:0] n_i_data [LO:HI][LO:HI], e_i_data [LO:HI][LO:HI],
                  s_i_data [LO:HI][LO:HI], w_i_data [LO:HI][LO:HI],
                  l_i_data [LO:HI][LO:HI];
    wire          n_i_val  [LO:HI][LO:HI], e_i_val  [LO:HI][LO:HI],
                  s_i_val  [LO:HI][LO:HI], w_i_val  [LO:HI][LO:HI],
                  l_i_val  [LO:HI][LO:HI];
    wire          n_i_busy [LO:HI][LO:HI], e_i_busy [LO:HI][LO:HI],
                  s_i_busy [LO:HI][LO:HI], w_i_busy [LO:HI][LO:HI],
                  l_i_busy [LO:HI][LO:HI];

    // local-port injection, driven by the stimulus process
    reg  [DW-1:0] inj_data  [LO:HI][LO:HI];
    reg           inj_valid [LO:HI][LO:HI];

    genvar gx, gy;
    generate
      for (gx = LO; gx <= HI; gx = gx + 1) begin : gen_x
        for (gy = LO; gy <= HI; gy = gy + 1) begin : gen_y

          NoCRouter #(
              .DATA_WIDTH(DW), .FIFO_DEPTH(32), .MEMORY_TYPE("distributed"),
              .POS_WIDTH(POSW), .POS_X(gx), .POS_Y(gy),
              .GRID_LO(LO), .GRID_HI(HI)
          ) rtr (
              .clk(clk), .rst(rst),
              .north_in_data (n_i_data[gx][gy]), .north_in_valid(n_i_val[gx][gy]),
              .north_in_busy (n_i_busy[gx][gy]),
              .north_out_data(n_o_data[gx][gy]), .north_out_valid(n_o_val[gx][gy]),
              .north_out_busy(n_o_busy[gx][gy]),
              .east_in_data  (e_i_data[gx][gy]), .east_in_valid (e_i_val[gx][gy]),
              .east_in_busy  (e_i_busy[gx][gy]),
              .east_out_data (e_o_data[gx][gy]), .east_out_valid(e_o_val[gx][gy]),
              .east_out_busy (e_o_busy[gx][gy]),
              .south_in_data (s_i_data[gx][gy]), .south_in_valid(s_i_val[gx][gy]),
              .south_in_busy (s_i_busy[gx][gy]),
              .south_out_data(s_o_data[gx][gy]), .south_out_valid(s_o_val[gx][gy]),
              .south_out_busy(s_o_busy[gx][gy]),
              .west_in_data  (w_i_data[gx][gy]), .west_in_valid (w_i_val[gx][gy]),
              .west_in_busy  (w_i_busy[gx][gy]),
              .west_out_data (w_o_data[gx][gy]), .west_out_valid(w_o_val[gx][gy]),
              .west_out_busy (w_o_busy[gx][gy]),
              .local_in_data (l_i_data[gx][gy]), .local_in_valid(l_i_val[gx][gy]),
              .local_in_busy (l_i_busy[gx][gy]),
              .local_out_data(l_o_data[gx][gy]), .local_out_valid(l_o_val[gx][gy]),
              .local_out_busy(l_o_busy[gx][gy])
          );

          // local port: injector in, receiver always ready out
          assign l_i_data[gx][gy] = inj_data[gx][gy];
          assign l_i_val [gx][gy] = inj_valid[gx][gy];
          assign l_o_busy[gx][gy] = 1'b0;

          // ---- neighbour wiring; unconnected sides become monitored edges ----
          if (gy > LO) begin : gy_up
            assign s_i_data[gx][gy-1] = n_o_data[gx][gy];
            assign s_i_val [gx][gy-1] = n_o_val [gx][gy];
            assign n_o_busy[gx][gy]   = s_i_busy[gx][gy-1];
          end else begin : gy_edge_n
            assign n_o_busy[gx][gy] = 1'b0;
            always @(posedge clk) if (!rst && n_o_val[gx][gy]) begin
              $display("[%0t] ROUTING ERROR: router(%0d,%0d) emitted north off-mesh, dst=(%0d,%0d)",
                       $time, gx, gy, n_o_data[gx][gy][DW-1 -: POSW],
                       n_o_data[gx][gy][DW-POSW-1 -: POSW]);
              errors = errors + 1;
            end
          end

          if (gy < HI) begin : gy_dn
            assign n_i_data[gx][gy+1] = s_o_data[gx][gy];
            assign n_i_val [gx][gy+1] = s_o_val [gx][gy];
            assign s_o_busy[gx][gy]   = n_i_busy[gx][gy+1];
          end else begin : gy_edge_s
            assign s_o_busy[gx][gy] = 1'b0;
            always @(posedge clk) if (!rst && s_o_val[gx][gy]) begin
              $display("[%0t] ROUTING ERROR: router(%0d,%0d) emitted south off-mesh, dst=(%0d,%0d)",
                       $time, gx, gy, s_o_data[gx][gy][DW-1 -: POSW],
                       s_o_data[gx][gy][DW-POSW-1 -: POSW]);
              errors = errors + 1;
            end
          end

          if (gx < HI) begin : gx_right
            assign w_i_data[gx+1][gy] = e_o_data[gx][gy];
            assign w_i_val [gx+1][gy] = e_o_val [gx][gy];
            assign e_o_busy[gx][gy]   = w_i_busy[gx+1][gy];
          end else begin : gx_edge_e
            assign e_o_busy[gx][gy] = 1'b0;
            always @(posedge clk) if (!rst && e_o_val[gx][gy]) begin
              $display("[%0t] ROUTING ERROR: router(%0d,%0d) emitted east off-mesh, dst=(%0d,%0d)",
                       $time, gx, gy, e_o_data[gx][gy][DW-1 -: POSW],
                       e_o_data[gx][gy][DW-POSW-1 -: POSW]);
              errors = errors + 1;
            end
          end

          if (gx > LO) begin : gx_left
            assign e_i_data[gx-1][gy] = w_o_data[gx][gy];
            assign e_i_val [gx-1][gy] = w_o_val [gx][gy];
            assign w_o_busy[gx][gy]   = e_i_busy[gx-1][gy];
          end else begin : gx_edge_w
            assign w_o_busy[gx][gy] = 1'b0;
            always @(posedge clk) if (!rst && w_o_val[gx][gy]) begin
              $display("[%0t] ROUTING ERROR: router(%0d,%0d) emitted west off-mesh, dst=(%0d,%0d)",
                       $time, gx, gy, w_o_data[gx][gy][DW-1 -: POSW],
                       w_o_data[gx][gy][DW-POSW-1 -: POSW]);
              errors = errors + 1;
            end
          end

          // tie off unconnected inputs
          if (gy == LO) begin : tie_n_in
            assign n_i_data[gx][gy] = {DW{1'b0}};
            assign n_i_val [gx][gy] = 1'b0;
          end
          if (gy == HI) begin : tie_s_in
            assign s_i_data[gx][gy] = {DW{1'b0}};
            assign s_i_val [gx][gy] = 1'b0;
          end
          if (gx == LO) begin : tie_w_in
            assign w_i_data[gx][gy] = {DW{1'b0}};
            assign w_i_val [gx][gy] = 1'b0;
          end
          if (gx == HI) begin : tie_e_in
            assign e_i_data[gx][gy] = {DW{1'b0}};
            assign e_i_val [gx][gy] = 1'b0;
          end

        end
      end
    endgenerate

    // ---------------------------------------------------------------- checkers
    // expected next sequence number per (src, dst) flat index pair
    integer exp_seq [0:NODES-1][0:NODES-1];
    integer nxt_seq [0:NODES-1][0:NODES-1];

    function integer flat;
        input integer x, y;
        begin flat = (x - LO) * N + (y - LO); end
    endfunction

    genvar rx, ry;
    generate
      for (rx = LO; rx <= HI; rx = rx + 1) begin : chk_x
        for (ry = LO; ry <= HI; ry = ry + 1) begin : chk_y
          always @(posedge clk) if (!rst && l_o_val[rx][ry]) begin : chk
            reg [POSW-1:0] dx, dy;
            integer src, dstf, seq;
            dx   = l_o_data[rx][ry][DW-1 -: POSW];
            dy   = l_o_data[rx][ry][DW-POSW-1 -: POSW];
            src  = l_o_data[rx][ry][SRC_LSB +: 8];
            seq  = l_o_data[rx][ry][SEQ_LSB +: 16];
            dstf = flat(rx, ry);

            if (dx !== rx[POSW-1:0] || dy !== ry[POSW-1:0]) begin
              $display("[%0t] MISDELIVERY: node(%0d,%0d) got a flit addressed to (%0d,%0d)",
                       $time, rx, ry, dx, dy);
              errors = errors + 1;
            end else if (seq !== exp_seq[src][dstf]) begin
              $display("[%0t] OUT OF ORDER: src %0d -> node(%0d,%0d) seq %0d, expected %0d",
                       $time, src, rx, ry, seq, exp_seq[src][dstf]);
              errors = errors + 1;
              exp_seq[src][dstf] = seq + 1;
            end else begin
              exp_seq[src][dstf] = exp_seq[src][dstf] + 1;
            end
            recv_total = recv_total + 1;
          end
        end
      end
    endgenerate

    // ---------------------------------------------------------------- stimulus
    integer seed = 32'h1234_5678;

    task automatic send;                 // one flit, respecting backpressure
        input integer sx, sy, dx, dy;
        integer sf, df;
        begin
            sf = flat(sx, sy);
            df = flat(dx, dy);
            @(negedge clk);
            while (l_i_busy[sx][sy]) @(negedge clk);
            inj_data[sx][sy]  = {DW{1'b0}};
            inj_data[sx][sy][DW-1        -: POSW] = dx[POSW-1:0];
            inj_data[sx][sy][DW-POSW-1   -: POSW] = dy[POSW-1:0];
            inj_data[sx][sy][DW-2*POSW-1 -: POSW] = sx[POSW-1:0];
            inj_data[sx][sy][DW-3*POSW-1 -: POSW] = sy[POSW-1:0];
            inj_data[sx][sy][SRC_LSB +: 8]  = sf[7:0];
            inj_data[sx][sy][SEQ_LSB +: 16] = nxt_seq[sf][df][15:0];
            inj_valid[sx][sy] = 1'b1;
            nxt_seq[sf][df] = nxt_seq[sf][df] + 1;
            sent_total = sent_total + 1;
            @(negedge clk);
            inj_valid[sx][sy] = 1'b0;
        end
    endtask

    integer i, j, k, x, y, dx, dy;

    initial begin
        $dumpfile("noc_mesh_tb.vcd");
        $dumpvars(0, noc_mesh_tb);

        for (i = 0; i < NODES; i = i + 1)
          for (j = 0; j < NODES; j = j + 1) begin
            exp_seq[i][j] = 0;
            nxt_seq[i][j] = 0;
          end
        for (x = LO; x <= HI; x = x + 1)
          for (y = LO; y <= HI; y = y + 1) begin
            inj_data[x][y]  = {DW{1'b0}};
            inj_valid[x][y] = 1'b0;
          end

        rst = 1; repeat (16) @(posedge clk);
        rst = 0; repeat (8)  @(posedge clk);

        $display("--- phase 1: all-to-all, one flit per pair ---");
        for (x = LO; x <= HI; x = x + 1)
          for (y = LO; y <= HI; y = y + 1)
            for (dx = LO; dx <= HI; dx = dx + 1)
              for (dy = LO; dy <= HI; dy = dy + 1)
                send(x, y, dx, dy);
        repeat (400) @(posedge clk);
        $display("    sent %0d, received %0d", sent_total, recv_total);

        $display("--- phase 2: diagonal flood (every packet needs an X and a Y hop) ---");
        // the pattern that closes a cycle under adaptive routing
        for (k = 0; k < 40; k = k + 1) begin
            send(LO, LO, HI, HI);
            send(HI, LO, LO, HI);
            send(HI, HI, LO, LO);
            send(LO, HI, HI, LO);
        end
        repeat (3000) @(posedge clk);
        $display("    sent %0d, received %0d", sent_total, recv_total);

        $display("--- phase 3: random traffic ---");
        for (k = 0; k < 200; k = k + 1) begin
            x  = LO + ({$random(seed)} % N);
            y  = LO + ({$random(seed)} % N);
            dx = LO + ({$random(seed)} % N);
            dy = LO + ({$random(seed)} % N);
            send(x, y, dx, dy);
        end
        repeat (6000) @(posedge clk);

        $display("");
        $display("========================================");
        $display("  sent %0d   received %0d   errors %0d", sent_total, recv_total, errors);
        if (sent_total != recv_total) begin
            $display("  FAIL -- %0d flits lost or stuck", sent_total - recv_total);
            $display("========================================");
            $finish;
        end
        if (errors != 0) begin
            $display("  FAIL -- %0d protocol/routing errors", errors);
            $display("========================================");
            $finish;
        end
        $display("  PASS -- lossless, correctly routed, in-order, no deadlock");
        $display("========================================");
        $finish;
    end

    // A deadlocked network does not produce a wrong answer, it produces no
    // answer. The watchdog is therefore a first-class check.
    initial begin
        #4_000_000;
        $display("");
        $display("========================================");
        $display("  FAIL -- WATCHDOG TIMEOUT");
        $display("  sent %0d, received %0d  (%0d in flight)",
                 sent_total, recv_total, sent_total - recv_total);
        $display("  a hang here is deadlock or lost backpressure");
        $display("========================================");
        $finish;
    end

endmodule
