// A surface tunnelled through AXI4-Stream: sender -> kts_over_axis -> four
// stream stages with random tready in both directions (flits and credits) ->
// kts_over_axis -> receiver. The stream's backpressure never reaches the
// surface; order, content and drain are checked, and the sender's credits
// never exceed what the receiver issued.

`timescale 1ns / 1ps
`default_nettype none

module kts_over_axis_tb;
    localparam integer W    = 64;
    localparam integer VC   = 2;
    localparam integer D    = 16;
    localparam integer CN_W = 4;
    localparam integer VCW  = 1;
    localparam integer UW   = VCW + 1;
    localparam integer CDW  = VCW + CN_W;
    localparam integer NST  = 4;

    reg clk = 1'b0;
    reg rst = 1'b1;
    always begin
        #1.667 clk = ~clk;
    end
    integer errors = 0;

    // ---- the surface's ends ----------------------------------------------------
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

    // ---- the two tunnel ends and the streams between them -----------------------
    // A: near the sender. Its flit stream goes to B; B's credit stream comes back.
    wire            af_v, af_r, bf_v, bf_r, ac_v, ac_r, bc_v, bc_r;
    wire [W-1:0]    af_d, bf_d;
    wire [UW-1:0]   af_u, bf_u;
    wire [CDW-1:0]  ac_d, bc_d;
    // A's unused inbound flit stream / outbound credit stream, B's likewise
    wire            a_sf_r, a_mc_v, a_mc_r_unused, b_mf_v, b_mf_r_unused, b_sc_r;
    wire [W-1:0]    b_mf_d;
    wire [UW-1:0]   b_mf_u;
    wire [CDW-1:0]  a_mc_d;
    wire            a_ov, a_ol, a_ocv;
    wire [VCW-1:0]  a_ovc, a_ocvc;
    wire [W-1:0]    a_of;
    wire [CN_W-1:0] a_ocn;
    wire            b_ocv;
    wire [VCW-1:0]  b_ocvc;
    wire [CN_W-1:0] b_ocn;

    kts_over_axis #(.W(W), .VC(VC), .D(D), .CN_W(CN_W)) u_a (
        .clk(clk), .rst(rst),
        .i_valid(t_v), .i_vc(t_vc), .i_last(t_l), .i_flit(t_f),
        .o_valid(a_ov), .o_vc(a_ovc), .o_last(a_ol), .o_flit(a_of),
        .i_crd_valid(1'b0), .i_crd_vc({VCW{1'b0}}), .i_crd_n({CN_W{1'b0}}),
        .o_crd_valid(tc_v), .o_crd_vc(tc_vc), .o_crd_n(tc_n),
        .mf_tvalid(af_v), .mf_tready(af_r), .mf_tdata(af_d), .mf_tuser(af_u),
        .sf_tvalid(1'b0), .sf_tready(a_sf_r), .sf_tdata({W{1'b0}}), .sf_tuser({UW{1'b0}}),
        .mc_tvalid(a_mc_v), .mc_tready(1'b1), .mc_tdata(a_mc_d),
        .sc_tvalid(bc_v), .sc_tready(bc_r), .sc_tdata(bc_d)
    );
    kts_over_axis #(.W(W), .VC(VC), .D(D), .CN_W(CN_W)) u_b (
        .clk(clk), .rst(rst),
        .i_valid(1'b0), .i_vc({VCW{1'b0}}), .i_last(1'b0), .i_flit({W{1'b0}}),
        .o_valid(r_v), .o_vc(r_vc), .o_last(r_l), .o_flit(r_f),
        .i_crd_valid(rc_v), .i_crd_vc(rc_vc), .i_crd_n(rc_n),
        .o_crd_valid(b_ocv), .o_crd_vc(b_ocvc), .o_crd_n(b_ocn),
        .mf_tvalid(b_mf_v), .mf_tready(1'b1), .mf_tdata(b_mf_d), .mf_tuser(b_mf_u),
        .sf_tvalid(bf_v), .sf_tready(bf_r), .sf_tdata(bf_d), .sf_tuser(bf_u),
        .mc_tvalid(ac_v), .mc_tready(ac_r), .mc_tdata(ac_d),
        .sc_tvalid(1'b0), .sc_tready(b_sc_r), .sc_tdata({CDW{1'b0}})
    );

    // NST register-slice stages with random tready, flits A->B and credits B->A
    reg  [W+UW-1:0] fs_d [0:NST-1];
    reg             fs_v [0:NST-1];
    reg  [CDW-1:0]  cs_d [0:NST-1];
    reg             cs_v [0:NST-1];
    reg  [NST-1:0]  f_stall, c_stall;        // a stage refusing this cycle
    integer s;
    // stage s accepts when not stalled and (empty or its successor takes);
    // the tail is taken by the tunnel end, whose tready is 1 by contract
    wire [NST:0] f_take;
    wire [NST:0] c_take;
    assign f_take[NST] = fs_v[NST-1] && bf_r;
    assign c_take[NST] = cs_v[NST-1] && bc_r;
    genvar gs;
    generate
    for (gs = 0; gs < NST; gs = gs + 1) begin : g_st
        wire f_in_v  = (gs == 0) ? af_v : fs_v[gs-1];
        wire c_in_v  = (gs == 0) ? ac_v : cs_v[gs-1];
        assign f_take[gs] = f_in_v && !f_stall[gs] && (!fs_v[gs] || f_take[gs+1]);
        assign c_take[gs] = c_in_v && !c_stall[gs] && (!cs_v[gs] || c_take[gs+1]);
    end
    endgenerate
    assign af_r = !f_stall[0] && (!fs_v[0] || f_take[1]);
    assign ac_r = !c_stall[0] && (!cs_v[0] || c_take[1]);
    assign bf_v = fs_v[NST-1];
    assign {bf_u, bf_d} = fs_d[NST-1];
    assign bc_v = cs_v[NST-1];
    assign bc_d = cs_d[NST-1];
    always @(posedge clk) begin
        for (s = 0; s < NST; s = s + 1) begin
            f_stall[s] <= ($urandom % 3 == 0);
            c_stall[s] <= ($urandom % 3 == 0);
        end
        for (s = 0; s < NST; s = s + 1) begin
            if (rst) begin
                fs_v[s] <= 1'b0;
                cs_v[s] <= 1'b0;
            end
            else begin
                if (f_take[s]) begin
                    fs_v[s] <= 1'b1;
                    fs_d[s] <= (s == 0) ? {af_u, af_d} : fs_d[s-1];
                end
                else if (f_take[s+1]) begin
                    fs_v[s] <= 1'b0;
                end
                if (c_take[s]) begin
                    cs_v[s] <= 1'b1;
                    cs_d[s] <= (s == 0) ? ac_d : cs_d[s-1];
                end
                else if (c_take[s+1]) begin
                    cs_v[s] <= 1'b0;
                end
            end
        end
    end
    // B's tready on flits and A's on credits are constant 1 by contract
    always @(posedge clk) if (!rst) begin
        if (bf_v && !bf_r) begin
            $display("%0t ERROR the tunnel end deasserted tready on the flit stream", $time);
            errors = errors + 1;
        end
        if (bc_v && !bc_r) begin
            $display("%0t ERROR the tunnel end deasserted tready on the credit stream", $time);
            errors = errors + 1;
        end
    end

    // ---- traffic ------------------------------------------------------------------
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
                        $display("%0t ERROR VC %0d: got %h, expected seq %0d", $time, k, out_flit[k*W +: W], seq_rx[k]);
                        errors = errors + 1;
                    end
                    seq_rx[k] <= seq_rx[k] + 1;
                end
            end
        end
    end

    integer j;
    initial begin
        repeat (5) @(posedge clk);
        rst <= 1'b0;
        repeat (6000) @(posedge clk);
        quiet <= 1'b1;
        repeat (600) @(posedge clk);
        for (j = 0; j < VC; j = j + 1) begin
            if (seq_rx[j] != seq_tx[j]) begin
                $display("ERROR VC %0d did not drain: tx %0d rx %0d", j, seq_tx[j], seq_rx[j]);
                errors = errors + 1;
            end
        end
        $display("kts_over_axis_tb: %0d/%0d flits (VC0/VC1) through %0d stream stages each way", seq_tx[0], seq_tx[1], NST);
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
