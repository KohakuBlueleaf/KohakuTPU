// A surface over a lossy word stream: sender -> kts_over_serial (A) -> channel
// model (random ready, random word DROPS) -> kts_over_serial (B) -> receiver,
// and B's credits and acks back through the same kind of channel. With
// RELIABLE=1 every flit must arrive exactly once, in order, despite the
// drops; a second, lossless pair at RELIABLE=0 runs alongside as the control.

`timescale 1ns / 1ps
`default_nettype none

module kts_over_serial_tb;
    localparam integer W    = 96;
    localparam integer VC   = 2;
    localparam integer D    = 16;
    localparam integer CN_W = 4;
    localparam integer VCW  = 1;
    localparam integer CW   = 64;

    reg clk = 1'b0;
    reg rst = 1'b1;
    always begin
        #1.667 clk = ~clk;
    end
    integer errors = 0;

    genvar s;
    generate
    for (s = 0; s < 2; s = s + 1) begin : g_s
        // s = 0: RELIABLE=1 over a channel that drops words; s = 1: RELIABLE=0, lossless
        localparam integer REL  = (s == 0) ? 1 : 0;
        localparam integer DROP = (s == 0) ? 40 : 0;     // 1 in DROP words dropped

        reg  [VC-1:0]   req_valid;
        reg  [VC*W-1:0] req_flit;
        wire [VC-1:0]   req_take;
        reg  [31:0]     seq_tx [0:VC-1];
        reg  [31:0]     seq_rx [0:VC-1];
        reg             quiet;
        wire            t_v, t_l, r_v, r_l, tc_v, rc_v;
        wire [VCW-1:0]  t_vc, r_vc, tc_vc, rc_vc;
        wire [W-1:0]    t_f, r_f;
        wire [CN_W-1:0] tc_n, rc_n;
        wire [VC-1:0]   out_valid, out_last;
        wire [VC*W-1:0] out_flit;
        reg  [VC-1:0]   out_pop;
        wire [VC*($clog2(D)+1)-1:0] credits;

        kts_tx #(.W(W), .VC(VC), .CMAX(D), .CN_W(CN_W)) u_tx (
            .clk(clk), .rst(rst),
            .req_valid(req_valid), .req_last({VC{1'b1}}), .req_flit(req_flit), .req_take(req_take),
            .tx_valid(t_v), .tx_vc(t_vc), .tx_last(t_l), .tx_flit(t_f),
            .crd_valid(tc_v), .crd_vc(tc_vc), .crd_n(tc_n), .credits(credits)
        );
        kts_rx #(.W(W), .VC(VC), .D(D), .CN_W(CN_W)) u_rx (
            .clk(clk), .rst(rst),
            .rx_valid(r_v), .rx_vc(r_vc), .rx_last(r_l), .rx_flit(r_f),
            .out_valid(out_valid), .out_last(out_last), .out_flit(out_flit), .out_pop(out_pop),
            .crd_valid(rc_v), .crd_vc(rc_vc), .crd_n(rc_n)
        );

        // A: flits in from the sender; B: credits in from the receiver
        wire            ab_v, ab_r, ab_l, ba_v, ba_r, ba_l;
        wire [CW-1:0]   ab_d, ba_d;
        wire            ab_rx_v, ab_rx_l, ba_rx_v, ba_rx_l;
        wire [CW-1:0]   ab_rx_d, ba_rx_d;
        wire            a_ov, a_ol, b_ocv;
        wire [VCW-1:0]  a_ovc, b_ocvc;
        wire [W-1:0]    a_of;
        wire [CN_W-1:0] b_ocn;

        kts_over_serial #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .CW(CW), .RELIABLE(REL), .WIN(16), .TIMEOUT(200)) u_a (
            .clk(clk), .rst(rst),
            .i_valid(t_v), .i_vc(t_vc), .i_last(t_l), .i_flit(t_f),
            .o_valid(a_ov), .o_vc(a_ovc), .o_last(a_ol), .o_flit(a_of),
            .i_crd_valid(1'b0), .i_crd_vc({VCW{1'b0}}), .i_crd_n({CN_W{1'b0}}),
            .o_crd_valid(tc_v), .o_crd_vc(tc_vc), .o_crd_n(tc_n),
            .c_tx_valid(ab_v), .c_tx_ready(ab_r), .c_tx_data(ab_d), .c_tx_last(ab_l),
            .c_rx_valid(ba_rx_v), .c_rx_data(ba_rx_d), .c_rx_last(ba_rx_l)
        );
        kts_over_serial #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .CW(CW), .RELIABLE(REL), .WIN(16), .TIMEOUT(200)) u_b (
            .clk(clk), .rst(rst),
            .i_valid(1'b0), .i_vc({VCW{1'b0}}), .i_last(1'b0), .i_flit({W{1'b0}}),
            .o_valid(r_v), .o_vc(r_vc), .o_last(r_l), .o_flit(r_f),
            .i_crd_valid(rc_v), .i_crd_vc(rc_vc), .i_crd_n(rc_n),
            .o_crd_valid(b_ocv), .o_crd_vc(b_ocvc), .o_crd_n(b_ocn),
            .c_tx_valid(ba_v), .c_tx_ready(ba_r), .c_tx_data(ba_d), .c_tx_last(ba_l),
            .c_rx_valid(ab_rx_v), .c_rx_data(ab_rx_d), .c_rx_last(ab_rx_l)
        );

        // the channel: random ready on the sending side, a word dropped now and
        // then, a register on the way; last rides with the word
        reg ab_rdy, ba_rdy;
        reg ab_q_v, ba_q_v, ab_q_l, ba_q_l;
        reg [CW-1:0] ab_q_d, ba_q_d;
        assign ab_r = ab_rdy;
        assign ba_r = ba_rdy;
        assign ab_rx_v = ab_q_v; assign ab_rx_d = ab_q_d; assign ab_rx_l = ab_q_l;
        assign ba_rx_v = ba_q_v; assign ba_rx_d = ba_q_d; assign ba_rx_l = ba_q_l;
        integer dropped;
        always @(posedge clk) begin
            ab_rdy <= ($urandom % 3 != 0);
            ba_rdy <= ($urandom % 3 != 0);
            if (rst) begin
                ab_q_v <= 1'b0; ba_q_v <= 1'b0; dropped <= 0;
            end
            else begin
                ab_q_v <= ab_v && ab_r && ((DROP == 0) || ($urandom % DROP != 0));
                if (ab_v && ab_r && (DROP != 0) && ($urandom % DROP == 0)) begin
                    dropped <= dropped + 1;
                end
                ab_q_d <= ab_d; ab_q_l <= ab_l;
                ba_q_v <= ba_v && ba_r && ((DROP == 0) || ($urandom % DROP != 0));
                ba_q_d <= ba_d; ba_q_l <= ba_l;
            end
        end

        integer k;
        always @(posedge clk) begin
            if (rst) begin
                req_valid <= {VC{1'b0}};
                out_pop   <= {VC{1'b0}};
                quiet     <= 1'b0;
                for (k = 0; k < VC; k = k + 1) begin
                    seq_tx[k] <= 0;
                    seq_rx[k] <= 0;
                    req_flit[k*W +: W] <= {k[31:0], 32'd0, 32'hface_0000};
                end
            end
            else begin
                for (k = 0; k < VC; k = k + 1) begin
                    if (req_take[k]) begin
                        seq_tx[k] <= seq_tx[k] + 1;
                        req_flit[k*W +: W] <= {k[31:0], seq_tx[k] + 32'd1, 32'hface_0000 | (seq_tx[k] + 32'd1)};
                        req_valid[k] <= !quiet && ($urandom % 3 != 0);
                    end
                    else if (!req_valid[k]) begin
                        req_valid[k] <= !quiet && ($urandom % 3 != 0);
                    end
                    out_pop[k] <= ($urandom % 3 != 0);
                    if (out_pop[k] && out_valid[k]) begin
                        if (out_flit[k*W +: W] !== {k[31:0], seq_rx[k], 32'hface_0000 | seq_rx[k]}) begin
                            $display("%0t ERROR pair %0d VC %0d: got %h, expected seq %0d", $time, s, k, out_flit[k*W +: W], seq_rx[k]);
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
        repeat (5) @(posedge clk);
        rst <= 1'b0;
        repeat (20000) @(posedge clk);
        g_s[0].quiet <= 1'b1;
        g_s[1].quiet <= 1'b1;
        repeat (6000) @(posedge clk);
        for (j = 0; j < VC; j = j + 1) begin
            if (g_s[0].seq_rx[j] != g_s[0].seq_tx[j]) begin
                $display("ERROR reliable pair VC %0d did not drain: tx %0d rx %0d", j, g_s[0].seq_tx[j], g_s[0].seq_rx[j]);
                errors = errors + 1;
            end
            if (g_s[1].seq_rx[j] != g_s[1].seq_tx[j]) begin
                $display("ERROR lossless pair VC %0d did not drain: tx %0d rx %0d", j, g_s[1].seq_tx[j], g_s[1].seq_rx[j]);
                errors = errors + 1;
            end
        end
        if (g_s[0].dropped == 0) begin
            $display("ERROR the channel dropped nothing; the reliable pair was not exercised");
            errors = errors + 1;
        end
        $display("kts_over_serial_tb: reliable %0d/%0d flits with %0d words dropped; lossless %0d/%0d",
                 g_s[0].seq_tx[0], g_s[0].seq_tx[1], g_s[0].dropped, g_s[1].seq_tx[0], g_s[1].seq_tx[1]);
        if (errors == 0) begin
            $display("PASS");
        end
        else begin
            $display("FAIL: %0d error(s)", errors);
        end
        $finish;
    end

endmodule

`default_nettype wire
