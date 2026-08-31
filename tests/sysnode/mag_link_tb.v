// mag_link_tb -- one crossing, both classes, at every carrier length it might
// be built at.
//
//    A (class 0 terminates at B, class 1 B would forward)
//      ── kts_pipe(N) ──► B          credit returns through the same pipe
//      ◄─ kts_pipe(N) ──             B sends no data at all, deliberately:
//                                    a link that only returns credit alongside
//                                    traffic deadlocks the moment one direction
//                                    goes quiet.
//
// THE LENGTH SWEEP IS THE POINT. The crossing's real latency is unknown until
// placement, and a credit depth right at one length and silently wrong at
// another is the defect this bench exists to catch, so four surfaces of
// different length run at once and the verdict is over all of them.

`timescale 1ns / 1ps
`default_nettype none

module mag_link_tb;
    localparam integer LW   = 288;
    localparam integer UW   = 96;
    localparam integer RXB  = 64;
    localparam integer MAXB = 32;
    localparam integer CN_W = 4;
    localparam integer NS   = 4;

    localparam integer U_KIND = 0, U_DMESH = 4, U_SMESH = 6, U_TXN = 8;
    localparam integer U_LEN = 16, U_ADDR = 32;
    localparam [3:0] K_MEM_WR = 4'h1, K_NOC_FLIT = 4'h2, K_DOORBELL = 4'h3;

    function integer pipe_n(input integer s);
        pipe_n = (s == 0) ? 0 : (s == 1) ? 2 : (s == 2) ? 8 : 16;
    endfunction

    reg clk = 0, resetn = 0;
    always begin
        #1.666 clk = ~clk;
    end

    integer errors = 0;
    integer checks = 0;
    reg [NS-1:0] done;

    genvar s;
    generate
    for (s = 0; s < NS; s = s + 1) begin : g_s
        localparam integer N = (s == 0) ? 0 : (s == 1) ? 2 : (s == 2) ? 8 : 16;

        reg  [UW-1:0] a_h0, a_h1;
        reg  [LW-1:0] a_d0, a_d1;
        reg           a_hv0, a_hv1, a_dv0, a_dv1, a_dl0, a_dl1;
        wire          a_hr0, a_hr1, a_dr0, a_dr1;

        wire [UW-1:0] b_rh0, b_rh1;
        wire [LW-1:0] b_rd0, b_rd1;
        wire          b_rhv0, b_rhv1, b_rdv0, b_rdv1, b_rdl0, b_rdl1;
        reg           b_rhr0, b_rhr1, b_rdr0, b_rdr1;

        // A's surface to B, and B's back to A. Each pipe carries its own
        // forward wire and the credit wire that answers it.
        wire            ao_v, ao_vc, ao_l, ao_cv, ao_cvc;
        wire [LW-1:0]   ao_f;
        wire [CN_W-1:0] ao_cn;
        wire            bi_v, bi_vc, bi_l, bi_cv, bi_cvc;
        wire [LW-1:0]   bi_f;
        wire [CN_W-1:0] bi_cn;
        wire            bo_v, bo_vc, bo_l, bo_cv, bo_cvc;
        wire [LW-1:0]   bo_f;
        wire [CN_W-1:0] bo_cn;
        wire            ai_v, ai_vc, ai_l, ai_cv, ai_cvc;
        wire [LW-1:0]   ai_f;
        wire [CN_W-1:0] ai_cn;

        wire [63:0] a_ctr_tx, a_ctr_rx, a_ctr_stall;
        wire [63:0] b_ctr_tx, b_ctr_rx, b_ctr_stall;
        wire [31:0] a_cred, b_cred;
        wire        a_flen, b_flen;

        mag_link #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB),
                   .MAX_BEATS(MAXB), .CN_W(CN_W)) A (
            .clk(clk), .resetn(resetn),
            .tx0_hdr(a_h0), .tx0_hvalid(a_hv0), .tx0_hready(a_hr0),
            .tx0_dat(a_d0), .tx0_dlast(a_dl0), .tx0_dvalid(a_dv0),
            .tx0_dready(a_dr0),
            .tx1_hdr(a_h1), .tx1_hvalid(a_hv1), .tx1_hready(a_hr1),
            .tx1_dat(a_d1), .tx1_dlast(a_dl1), .tx1_dvalid(a_dv1),
            .tx1_dready(a_dr1),
            .rx0_hdr(), .rx0_hvalid(), .rx0_hready(1'b1),
            .rx0_dat(), .rx0_dlast(), .rx0_dvalid(), .rx0_dready(1'b1),
            .rx1_hdr(), .rx1_hvalid(), .rx1_hready(1'b1),
            .rx1_dat(), .rx1_dlast(), .rx1_dvalid(), .rx1_dready(1'b1),
            .o_valid(ao_v), .o_vc(ao_vc), .o_last(ao_l), .o_flit(ao_f),
            .o_crd_valid(ao_cv), .o_crd_vc(ao_cvc), .o_crd_n(ao_cn),
            .i_valid(ai_v), .i_vc(ai_vc), .i_last(ai_l), .i_flit(ai_f),
            .i_crd_valid(ai_cv), .i_crd_vc(ai_cvc), .i_crd_n(ai_cn),
            .ctr_tx(a_ctr_tx), .ctr_rx(a_ctr_rx), .ctr_stall(a_ctr_stall),
            .cred_state(a_cred), .fault_len(a_flen)
        );

        mag_link #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB),
                   .MAX_BEATS(MAXB), .CN_W(CN_W)) B (
            .clk(clk), .resetn(resetn),
            .tx0_hdr({UW{1'b0}}), .tx0_hvalid(1'b0), .tx0_hready(),
            .tx0_dat({LW{1'b0}}), .tx0_dlast(1'b0), .tx0_dvalid(1'b0),
            .tx0_dready(),
            .tx1_hdr({UW{1'b0}}), .tx1_hvalid(1'b0), .tx1_hready(),
            .tx1_dat({LW{1'b0}}), .tx1_dlast(1'b0), .tx1_dvalid(1'b0),
            .tx1_dready(),
            .rx0_hdr(b_rh0), .rx0_hvalid(b_rhv0), .rx0_hready(b_rhr0),
            .rx0_dat(b_rd0), .rx0_dlast(b_rdl0), .rx0_dvalid(b_rdv0),
            .rx0_dready(b_rdr0),
            .rx1_hdr(b_rh1), .rx1_hvalid(b_rhv1), .rx1_hready(b_rhr1),
            .rx1_dat(b_rd1), .rx1_dlast(b_rdl1), .rx1_dvalid(b_rdv1),
            .rx1_dready(b_rdr1),
            .o_valid(bo_v), .o_vc(bo_vc), .o_last(bo_l), .o_flit(bo_f),
            .o_crd_valid(bo_cv), .o_crd_vc(bo_cvc), .o_crd_n(bo_cn),
            .i_valid(bi_v), .i_vc(bi_vc), .i_last(bi_l), .i_flit(bi_f),
            .i_crd_valid(bi_cv), .i_crd_vc(bi_cvc), .i_crd_n(bi_cn),
            .ctr_tx(b_ctr_tx), .ctr_rx(b_ctr_rx), .ctr_stall(b_ctr_stall),
            .cred_state(b_cred), .fault_len(b_flen)
        );

        kts_pipe #(.W(LW), .VCW(1), .CN_W(CN_W), .N(N)) P_AB (
            .clk(clk), .rst(!resetn),
            .i_valid(ao_v), .i_vc(ao_vc), .i_last(ao_l), .i_flit(ao_f),
            .o_valid(bi_v), .o_vc(bi_vc), .o_last(bi_l), .o_flit(bi_f),
            .i_crd_valid(bi_cv), .i_crd_vc(bi_cvc), .i_crd_n(bi_cn),
            .o_crd_valid(ao_cv), .o_crd_vc(ao_cvc), .o_crd_n(ao_cn)
        );

        kts_pipe #(.W(LW), .VCW(1), .CN_W(CN_W), .N(N)) P_BA (
            .clk(clk), .rst(!resetn),
            .i_valid(bo_v), .i_vc(bo_vc), .i_last(bo_l), .i_flit(bo_f),
            .o_valid(ai_v), .o_vc(ai_vc), .o_last(ai_l), .o_flit(ai_f),
            .i_crd_valid(ai_cv), .i_crd_vc(ai_cvc), .i_crd_n(ai_cn),
            .o_crd_valid(bo_cv), .o_crd_vc(bo_cvc), .o_crd_n(bo_cn)
        );

        // ---------------------------------------------------------- expected
        localparam integer QD = 64;
        reg [UW-1:0] exp_h0 [0:QD-1];
        reg [UW-1:0] exp_h1 [0:QD-1];
        reg [31:0]   exp_s0 [0:QD-1];
        reg [31:0]   exp_s1 [0:QD-1];
        reg [15:0]   exp_l0 [0:QD-1];
        reg [15:0]   exp_l1 [0:QD-1];
        integer wr0, rd0, wr1, rd1;
        integer t, n, stall_before;

        task fail(input [1023:0] why);
            begin
                errors = errors + 1;
                $display("  FAIL (pipe %0d): %0s", N, why);
            end
        endtask

        task automatic send0(input [3:0] kind, input [1:0] dm, input [7:0] txn,
                             input [15:0] len, input [33:0] addr,
                             input [31:0] seed);
            integer i;
            begin
                exp_h0[wr0] = hdr(kind, dm, txn, len, addr);
                exp_s0[wr0] = seed;
                exp_l0[wr0] = len;
                wr0 = wr0 + 1;
                a_h0  <= hdr(kind, dm, txn, len, addr);
                a_hv0 <= 1'b1;
                @(posedge clk);
                while (!a_hr0) begin
                    @(posedge clk);
                end
                a_hv0 <= 1'b0;
                for (i = 0; i <= len; i = i + 1) begin
                    a_d0  <= patt(seed, i[15:0]);
                    a_dl0 <= (i == len);
                    a_dv0 <= 1'b1;
                    @(posedge clk);
                    while (!a_dr0) begin
                        @(posedge clk);
                    end
                end
                a_dv0 <= 1'b0;
                a_dl0 <= 1'b0;
            end
        endtask

        task automatic send1(input [3:0] kind, input [1:0] dm, input [7:0] txn,
                             input [15:0] len, input [33:0] addr,
                             input [31:0] seed);
            integer i;
            begin
                exp_h1[wr1] = hdr(kind, dm, txn, len, addr);
                exp_s1[wr1] = seed;
                exp_l1[wr1] = len;
                wr1 = wr1 + 1;
                a_h1  <= hdr(kind, dm, txn, len, addr);
                a_hv1 <= 1'b1;
                @(posedge clk);
                while (!a_hr1) begin
                    @(posedge clk);
                end
                a_hv1 <= 1'b0;
                for (i = 0; i <= len; i = i + 1) begin
                    a_d1  <= patt(seed, i[15:0]);
                    a_dl1 <= (i == len);
                    a_dv1 <= 1'b1;
                    @(posedge clk);
                    while (!a_dr1) begin
                        @(posedge clk);
                    end
                end
                a_dv1 <= 1'b0;
                a_dl1 <= 1'b0;
            end
        endtask

        task automatic recv0;
            integer i;
            reg [UW-1:0] got_h;
            reg [15:0]   want_len;
            reg [31:0]   want_seed;
            begin
                b_rhr0 <= 1'b1;
                @(posedge clk);
                while (!b_rhv0) begin
                    @(posedge clk);
                end
                got_h = b_rh0;
                b_rhr0 <= 1'b0;
                checks = checks + 1;
                if (rd0 >= wr0) begin
                    fail("a packet arrived on class 0 that was never sent");
                end
                else begin
                    if (got_h !== exp_h0[rd0]) begin
                        fail("class 0 header mismatch");
                        $display("        got  %h", got_h);
                        $display("        want %h", exp_h0[rd0]);
                    end
                    want_seed = exp_s0[rd0];
                    want_len  = exp_l0[rd0];
                    rd0 = rd0 + 1;
                    b_rdr0 <= 1'b1;
                    for (i = 0; i <= want_len; i = i + 1) begin
                        @(posedge clk);
                        while (!b_rdv0) begin
                            @(posedge clk);
                        end
                        checks = checks + 1;
                        if (b_rd0 !== patt(want_seed, i[15:0])) begin
                            fail("class 0 payload mismatch");
                        end
                        if (b_rdl0 !== (i == want_len)) begin
                            fail("class 0 last is on the wrong beat");
                        end
                    end
                    b_rdr0 <= 1'b0;
                end
            end
        endtask

        task automatic recv1;
            integer i;
            reg [UW-1:0] got_h;
            reg [15:0]   want_len;
            reg [31:0]   want_seed;
            begin
                b_rhr1 <= 1'b1;
                @(posedge clk);
                while (!b_rhv1) begin
                    @(posedge clk);
                end
                got_h = b_rh1;
                b_rhr1 <= 1'b0;
                checks = checks + 1;
                if (rd1 >= wr1) begin
                    fail("a packet arrived on class 1 that was never sent");
                end
                else begin
                    if (got_h !== exp_h1[rd1]) begin
                        fail("class 1 header mismatch");
                    end
                    want_seed = exp_s1[rd1];
                    want_len  = exp_l1[rd1];
                    rd1 = rd1 + 1;
                    b_rdr1 <= 1'b1;
                    for (i = 0; i <= want_len; i = i + 1) begin
                        @(posedge clk);
                        while (!b_rdv1) begin
                            @(posedge clk);
                        end
                        checks = checks + 1;
                        if (b_rd1 !== patt(want_seed, i[15:0])) begin
                            fail("class 1 payload mismatch");
                        end
                    end
                    b_rdr1 <= 1'b0;
                end
            end
        endtask

        task reset_all;
            begin
                a_hv0 <= 0; a_hv1 <= 0; a_dv0 <= 0; a_dv1 <= 0;
                a_dl0 <= 0; a_dl1 <= 0;
                b_rhr0 <= 0; b_rhr1 <= 0; b_rdr0 <= 0; b_rdr1 <= 0;
                a_h0 <= 0; a_h1 <= 0; a_d0 <= 0; a_d1 <= 0;
                wr0 = 0; rd0 = 0; wr1 = 0; rd1 = 0;
                @(posedge clk);
            end
        endtask

        initial begin
            done[s] = 1'b0;
            reset_all;
            while (!resetn) begin
                @(posedge clk);
            end
            // The receiver announces its depth as ordinary credit once its
            // buffers leave reset, so nothing may be offered before that.
            repeat (2 * N + 24) @(posedge clk);

            // ---- 1. every kind, len 0 / 1 / many, on the terminating class
            fork
                begin
                    send0(K_MEM_WR,   2'd1, 8'h11, 16'd0,  34'h1_0000_0040, 32'hA1);
                    send0(K_NOC_FLIT, 2'd1, 8'h22, 16'd1,  34'h0,           32'hB2);
                    send0(K_DOORBELL, 2'd1, 8'h33, 16'd0,  34'h0,           32'hC3);
                    send0(K_MEM_WR,   2'd1, 8'h44, 16'd31, 34'h1_0000_0800, 32'hD4);
                end
                begin
                    recv0; recv0; recv0; recv0;
                end
            join

            // ---- 2. the forwarded class, same shapes
            fork
                begin
                    send1(K_MEM_WR, 2'd2, 8'h55, 16'd0,  34'h2_0000_0000, 32'hE5);
                    send1(K_MEM_WR, 2'd2, 8'h66, 16'd31, 34'h2_0000_1000, 32'hF6);
                end
                begin
                    recv1; recv1;
                end
            join

            // ---- 3. credit exhausts and recovers. A 32-beat packet is 33
            // flits against 64 credits, so two fit and the rest cannot move
            // until the receiver frees space.
            wr0 = 0; rd0 = 0;
            stall_before = a_ctr_stall[31:0];
            fork
                begin
                    for (n = 0; n < 4; n = n + 1) begin
                        send0(K_MEM_WR, 2'd1, n[7:0], MAXB[15:0] - 16'd1,
                              34'h1_0000_0000 + n * 64 * MAXB, 32'h1000 + n);
                    end
                end
                begin
                    repeat (400) @(posedge clk);
                    if (a_ctr_stall[31:0] == stall_before) begin
                        fail("the link never stalled -- credit is bounding nothing");
                    end
                    recv0; recv0; recv0; recv0;
                end
            join
            if (rd0 != 4) begin
                fail("packets were lost across the credit stall");
            end

            // ---- 4. class isolation, protocol.md s4(b). The forward class is
            // filled and never drained; the terminating class must keep
            // moving, and stopping it would be a deadlock, not a slowdown.
            wr0 = 0; rd0 = 0; wr1 = 0; rd1 = 0;
            fork
                begin
                    for (n = 0; n < 4; n = n + 1) begin
                        send1(K_MEM_WR, 2'd2, n[7:0], MAXB[15:0] - 16'd1,
                              34'h2_0000_0000 + n * 64 * MAXB, 32'h2000 + n);
                    end
                end
                begin
                    repeat (400) @(posedge clk);
                    send0(K_MEM_WR, 2'd1, 8'h77, 16'd3, 34'h1_0000_0000, 32'h77);
                    send0(K_MEM_WR, 2'd1, 8'h78, 16'd3, 34'h1_0000_0100, 32'h78);
                end
                begin
                    repeat (440) @(posedge clk);
                    recv0; recv0;
                    if (rd0 != 2) begin
                        fail("a full forward class stopped terminating traffic -- the classes share credit");
                    end
                    recv1; recv1; recv1; recv1;
                end
            join

            if (a_flen || b_flen) begin
                fail("a length fault was raised by a packet within MAX_BEATS");
            end
            $display("  pipe %0d: %0d + %0d packets delivered, %0d credit-stalled cycles",
                     N, b_ctr_rx[31:0], rd1, a_ctr_stall[31:0]);
            done[s] = 1'b1;
        end
    end
    endgenerate

    function [LW-1:0] patt(input [31:0] s_in, input [15:0] i);
        integer k;
        begin
            for (k = 0; k < LW/32; k = k + 1) begin
                patt[k*32 +: 32] = s_in + {16'd0, i} * 32'd7 + k[31:0];
            end
        end
    endfunction

    function [UW-1:0] hdr(input [3:0] kind, input [1:0] dm, input [7:0] txn,
                          input [15:0] len, input [33:0] addr);
        begin
            hdr = {UW{1'b0}};
            hdr[U_KIND  +: 4]  = kind;
            hdr[U_DMESH +: 2]  = dm;
            hdr[U_TXN   +: 8]  = txn;
            hdr[U_LEN   +: 16] = len;
            hdr[U_ADDR  +: 34] = addr;
        end
    endfunction

    initial begin
        $display("=== mag_link on the surface: pipes 0, 2, 8, 16 ===");
        done = {NS{1'b0}};
        repeat (8) @(posedge clk);
        resetn <= 1'b1;
        while (done !== {NS{1'b1}}) begin
            @(posedge clk);
        end
        $display("--- %0d checks, %0d errors", checks, errors);
        if (errors == 0) begin
            $display("PASS mag_link");
        end
        else begin
            $display("FAIL mag_link");
        end
        $finish;
    end

    initial begin
        #8_000_000;
        $display("WATCHDOG mag_link_tb -- no verdict. The link stopped making progress.");
        $display("FAIL mag_link");
        $finish;
    end
endmodule

`default_nettype wire
