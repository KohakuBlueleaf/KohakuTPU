// A station hop's two classes over THREE different carriers, from one pair of
// surface ends: register stages, an AXI4-Stream tunnel, and a lossy serial word
// channel with go-back-N. The ends never change; only what sits between them
// does, which is the property a credit MESSAGE has and a matched-depth pulse
// (sb_link) or a synchronised gray pointer (sb_link_cdc) does not.
//
// Widths are the station's own: RQW and RSW as sb_line4 computes them at
// FW = 256, AW = 43.

`timescale 1ns / 1ps
`default_nettype none

module sb_kts_carrier_tb;
    localparam integer FW   = 256;
    localparam integer AW   = 43;
    localparam integer RQW  = 2 + 3 + 4 + 3 + AW + 8 + 3 + FW + FW/8;
    localparam integer RSW  = 2 + 4 + 2 + 2 + FW;
    localparam integer W    = (RQW > RSW) ? RQW : RSW;
    localparam integer VC   = 2;
    localparam integer D    = 16;
    localparam integer CN_W = 4;
    localparam integer CW   = 64;
    localparam integer NC   = 3;            // pipe, stream, serial

    reg clk = 1'b0;
    reg rst = 1'b1;
    always begin
        #1.667 clk = ~clk;
    end
    integer errors = 0;

    genvar c;
    generate
    for (c = 0; c < NC; c = c + 1) begin : g_c
        // ---- the two ends, identical for every carrier ---------------------
        reg  [VC-1:0]   req_valid;
        reg  [VC*W-1:0] req_flit;
        wire [VC-1:0]   req_take;
        reg  [31:0]     seq_tx [0:VC-1];
        reg  [31:0]     seq_rx [0:VC-1];
        reg             quiet;

        wire            t_v, t_l, r_v, r_l, tc_v, rc_v;
        wire            t_vc, r_vc, tc_vc, rc_vc;
        wire [W-1:0]    t_f, r_f;
        wire [CN_W-1:0] tc_n, rc_n;
        wire [VC-1:0]   out_valid;
        wire [VC*W-1:0] out_flit;
        reg  [VC-1:0]   out_pop;

        kts_tx #(.W(W), .VC(VC), .CMAX(D), .CN_W(CN_W)) u_tx (
            .clk(clk), .rst(rst),
            .req_valid(req_valid), .req_last({VC{1'b1}}), .req_flit(req_flit),
            .req_take(req_take),
            .tx_valid(t_v), .tx_vc(t_vc), .tx_last(t_l), .tx_flit(t_f),
            .crd_valid(tc_v), .crd_vc(tc_vc), .crd_n(tc_n), .credits()
        );
        kts_rx #(.W(W), .VC(VC), .D(D), .CN_W(CN_W)) u_rx (
            .clk(clk), .rst(rst),
            .rx_valid(r_v), .rx_vc(r_vc), .rx_last(r_l), .rx_flit(r_f),
            .out_valid(out_valid), .out_last(), .out_flit(out_flit),
            .out_pop(out_pop),
            .crd_valid(rc_v), .crd_vc(rc_vc), .crd_n(rc_n)
        );

        // ---- the carrier ---------------------------------------------------
        if (c == 0) begin : g_pipe
            kts_pipe #(.W(W), .VCW(1), .CN_W(CN_W), .N(6)) u_p (
                .clk(clk), .rst(rst),
                .i_valid(t_v), .i_vc(t_vc), .i_last(t_l), .i_flit(t_f),
                .o_valid(r_v), .o_vc(r_vc), .o_last(r_l), .o_flit(r_f),
                .i_crd_valid(rc_v), .i_crd_vc(rc_vc), .i_crd_n(rc_n),
                .o_crd_valid(tc_v), .o_crd_vc(tc_vc), .o_crd_n(tc_n)
            );
        end
        else if (c == 1) begin : g_axis
            wire           af_v, af_r, ac_v, ac_r;
            wire [W-1:0]   af_d;
            wire [1:0]     af_u;
            wire [CN_W:0]  ac_d;
            wire           a_ov, a_ol, b_ocv;
            wire           a_ovc, b_ocvc;
            wire [W-1:0]   a_of;
            wire [CN_W-1:0] b_ocn;

            kts_over_axis #(.W(W), .VC(VC), .D(D), .CN_W(CN_W)) u_a (
                .clk(clk), .rst(rst),
                .i_valid(t_v), .i_vc(t_vc), .i_last(t_l), .i_flit(t_f),
                .o_valid(a_ov), .o_vc(a_ovc), .o_last(a_ol), .o_flit(a_of),
                .i_crd_valid(1'b0), .i_crd_vc(1'b0), .i_crd_n({CN_W{1'b0}}),
                .o_crd_valid(tc_v), .o_crd_vc(tc_vc), .o_crd_n(tc_n),
                .mf_tvalid(af_v), .mf_tready(af_r), .mf_tdata(af_d),
                .mf_tuser(af_u),
                .sf_tvalid(1'b0), .sf_tready(), .sf_tdata({W{1'b0}}),
                .sf_tuser(2'd0),
                .mc_tvalid(), .mc_tready(1'b1), .mc_tdata(),
                .sc_tvalid(ac_v), .sc_tready(ac_r), .sc_tdata(ac_d)
            );
            kts_over_axis #(.W(W), .VC(VC), .D(D), .CN_W(CN_W)) u_b (
                .clk(clk), .rst(rst),
                .i_valid(1'b0), .i_vc(1'b0), .i_last(1'b0), .i_flit({W{1'b0}}),
                .o_valid(r_v), .o_vc(r_vc), .o_last(r_l), .o_flit(r_f),
                .i_crd_valid(rc_v), .i_crd_vc(rc_vc), .i_crd_n(rc_n),
                .o_crd_valid(b_ocv), .o_crd_vc(b_ocvc), .o_crd_n(b_ocn),
                .mf_tvalid(), .mf_tready(1'b1), .mf_tdata(), .mf_tuser(),
                .sf_tvalid(af_v), .sf_tready(af_r), .sf_tdata(af_d),
                .sf_tuser(af_u),
                .mc_tvalid(ac_v), .mc_tready(ac_r), .mc_tdata(ac_d),
                .sc_tvalid(1'b0), .sc_tready(), .sc_tdata({(CN_W+1){1'b0}})
            );
        end
        else begin : g_serial
            // 1 word in 40 dropped; RELIABLE recovers by go-back-N.
            wire          ab_v, ab_r, ab_l, ba_v, ba_r, ba_l;
            wire [CW-1:0] ab_d, ba_d;
            reg           ab_q_v, ba_q_v, ab_q_l, ba_q_l, ab_rdy, ba_rdy;
            reg  [CW-1:0] ab_q_d, ba_q_d;
            wire          a_ov, a_ol, b_ocv, a_ovc, b_ocvc;
            wire [W-1:0]  a_of;
            wire [CN_W-1:0] b_ocn;
            integer       dropped;

            assign ab_r = ab_rdy;
            assign ba_r = ba_rdy;
            always @(posedge clk) begin
                ab_rdy <= ($urandom % 3 != 0);
                ba_rdy <= ($urandom % 3 != 0);
                if (rst) begin
                    ab_q_v <= 1'b0; ba_q_v <= 1'b0; dropped <= 0;
                end
                else begin
                    ab_q_v <= ab_v && ab_r && ($urandom % 40 != 0);
                    if (ab_v && ab_r && ($urandom % 40 == 0)) begin
                        dropped <= dropped + 1;
                    end
                    ab_q_d <= ab_d; ab_q_l <= ab_l;
                    ba_q_v <= ba_v && ba_r && ($urandom % 40 != 0);
                    ba_q_d <= ba_d; ba_q_l <= ba_l;
                end
            end

            kts_over_serial #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .CW(CW),
                              .RELIABLE(1), .WIN(16), .TIMEOUT(200)) u_a (
                .clk(clk), .rst(rst),
                .i_valid(t_v), .i_vc(t_vc), .i_last(t_l), .i_flit(t_f),
                .o_valid(a_ov), .o_vc(a_ovc), .o_last(a_ol), .o_flit(a_of),
                .i_crd_valid(1'b0), .i_crd_vc(1'b0), .i_crd_n({CN_W{1'b0}}),
                .o_crd_valid(tc_v), .o_crd_vc(tc_vc), .o_crd_n(tc_n),
                .c_tx_valid(ab_v), .c_tx_ready(ab_r), .c_tx_data(ab_d),
                .c_tx_last(ab_l),
                .c_rx_valid(ba_q_v), .c_rx_data(ba_q_d), .c_rx_last(ba_q_l)
            );
            kts_over_serial #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .CW(CW),
                              .RELIABLE(1), .WIN(16), .TIMEOUT(200)) u_b (
                .clk(clk), .rst(rst),
                .i_valid(1'b0), .i_vc(1'b0), .i_last(1'b0), .i_flit({W{1'b0}}),
                .o_valid(r_v), .o_vc(r_vc), .o_last(r_l), .o_flit(r_f),
                .i_crd_valid(rc_v), .i_crd_vc(rc_vc), .i_crd_n(rc_n),
                .o_crd_valid(b_ocv), .o_crd_vc(b_ocvc), .o_crd_n(b_ocn),
                .c_tx_valid(ba_v), .c_tx_ready(ba_r), .c_tx_data(ba_d),
                .c_tx_last(ba_l),
                .c_rx_valid(ab_q_v), .c_rx_data(ab_q_d), .c_rx_last(ab_q_l)
            );
        end

        // ---- traffic: class 0 at RQW, class 1 at RSW -----------------------
        integer k;
        always @(posedge clk) begin
            if (rst) begin
                req_valid <= {VC{1'b0}};
                out_pop   <= {VC{1'b0}};
                quiet     <= 1'b0;
                for (k = 0; k < VC; k = k + 1) begin
                    seq_tx[k] <= 0;
                    seq_rx[k] <= 0;
                    req_flit[k*W +: W] <= {k[31:0], 32'd0};
                end
            end
            else begin
                for (k = 0; k < VC; k = k + 1) begin
                    if (req_take[k]) begin
                        seq_tx[k] <= seq_tx[k] + 1;
                        req_flit[k*W +: W] <= {k[31:0], seq_tx[k] + 32'd1};
                        req_valid[k] <= !quiet && ($urandom % 3 != 0);
                    end
                    else if (!req_valid[k]) begin
                        req_valid[k] <= !quiet && ($urandom % 3 != 0);
                    end
                    out_pop[k] <= ($urandom % 3 != 0);
                    if (out_pop[k] && out_valid[k]) begin
                        if (out_flit[k*W +: W] !== {k[31:0], seq_rx[k]}) begin
                            $display("%0t ERROR carrier %0d VC %0d: got %h, want seq %0d",
                                     $time, c, k, out_flit[k*W +: W], seq_rx[k]);
                            errors = errors + 1;
                        end
                        seq_rx[k] <= seq_rx[k] + 1;
                    end
                end
            end
        end
    end
    endgenerate

    integer j;
    initial begin
        $display("=== a station hop's two classes over pipe / stream / serial ===");
        repeat (5) @(posedge clk);
        rst <= 1'b0;
        repeat (30000) @(posedge clk);
        g_c[0].quiet <= 1'b1;
        g_c[1].quiet <= 1'b1;
        g_c[2].quiet <= 1'b1;
        repeat (12000) @(posedge clk);

        for (j = 0; j < VC; j = j + 1) begin
            if (g_c[0].seq_rx[j] != g_c[0].seq_tx[j]) begin
                $display("ERROR pipe VC %0d did not drain: tx %0d rx %0d",
                         j, g_c[0].seq_tx[j], g_c[0].seq_rx[j]);
                errors = errors + 1;
            end
            if (g_c[1].seq_rx[j] != g_c[1].seq_tx[j]) begin
                $display("ERROR stream VC %0d did not drain: tx %0d rx %0d",
                         j, g_c[1].seq_tx[j], g_c[1].seq_rx[j]);
                errors = errors + 1;
            end
            if (g_c[2].seq_rx[j] != g_c[2].seq_tx[j]) begin
                $display("ERROR serial VC %0d did not drain: tx %0d rx %0d",
                         j, g_c[2].seq_tx[j], g_c[2].seq_rx[j]);
                errors = errors + 1;
            end
        end
        if (g_c[2].g_serial.dropped == 0) begin
            $display("ERROR the serial channel dropped nothing, so recovery was never exercised");
            errors = errors + 1;
        end

        $display("  RQW %0d RSW %0d, flit %0d", RQW, RSW, W);
        $display("  pipe   %0d + %0d flits", g_c[0].seq_tx[0], g_c[0].seq_tx[1]);
        $display("  stream %0d + %0d flits", g_c[1].seq_tx[0], g_c[1].seq_tx[1]);
        $display("  serial %0d + %0d flits, %0d words dropped",
                 g_c[2].seq_tx[0], g_c[2].seq_tx[1], g_c[2].g_serial.dropped);
        if (errors == 0) begin
            $display("PASS sb_kts_carrier");
        end
        else begin
            $display("FAIL sb_kts_carrier: %0d error(s)", errors);
        end
        $finish;
    end

    initial begin
        #20_000_000;
        $display("WATCHDOG sb_kts_carrier -- a carrier stopped making progress");
        $display("FAIL sb_kts_carrier");
        $finish;
    end
endmodule

`default_nettype wire
