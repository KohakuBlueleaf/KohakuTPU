// AXI4-Stream tunnelled through a surface: a source drives beats with tlast
// and tdest (the VC) into kts_axis_in, twelve register stages each way, and
// kts_axis_out delivers them to a sink with random tready. Content, order per
// tdest and tlast positions are checked.

`timescale 1ns / 1ps
`default_nettype none

module kts_axis_tb;
    localparam integer W    = 64;
    localparam integer VC   = 2;
    localparam integer D    = 16;
    localparam integer CN_W = 4;
    localparam integer VCW  = 1;
    localparam integer NPIPE = 12;

    reg clk = 1'b0;
    reg rst = 1'b1;
    always begin
        #1.667 clk = ~clk;
    end
    integer errors = 0;

    reg              s_tvalid;
    wire             s_tready;
    reg  [W-1:0]     s_tdata;
    reg              s_tlast;
    reg  [VCW-1:0]   s_tdest;

    wire a_v, a_l, p_v, p_l, a_cv, p_cv;
    wire [VCW-1:0] a_vc, p_vc, a_cvc, p_cvc;
    wire [W-1:0] a_f, p_f;
    wire [CN_W-1:0] a_cn, p_cn;

    kts_axis_in #(.W(W), .VC(VC), .CMAX(D), .CN_W(CN_W), .TDEST_W(VCW)) u_in (
        .clk(clk), .rst(rst),
        .s_tvalid(s_tvalid), .s_tready(s_tready), .s_tdata(s_tdata), .s_tlast(s_tlast), .s_tdest(s_tdest),
        .tx_valid(a_v), .tx_vc(a_vc), .tx_last(a_l), .tx_flit(a_f),
        .crd_valid(p_cv), .crd_vc(p_cvc), .crd_n(p_cn)
    );
    kts_pipe #(.W(W), .VCW(VCW), .CN_W(CN_W), .N(NPIPE)) u_p (
        .clk(clk), .rst(rst),
        .i_valid(a_v), .i_vc(a_vc), .i_last(a_l), .i_flit(a_f),
        .o_valid(p_v), .o_vc(p_vc), .o_last(p_l), .o_flit(p_f),
        .i_crd_valid(a_cv), .i_crd_vc(a_cvc), .i_crd_n(a_cn),
        .o_crd_valid(p_cv), .o_crd_vc(p_cvc), .o_crd_n(p_cn)
    );
    wire             m_tvalid;
    reg              m_tready;
    wire [W-1:0]     m_tdata;
    wire             m_tlast;
    wire [VCW-1:0]   m_tdest;
    kts_axis_out #(.W(W), .VC(VC), .D(D), .CN_W(CN_W)) u_out (
        .clk(clk), .rst(rst),
        .rx_valid(p_v), .rx_vc(p_vc), .rx_last(p_l), .rx_flit(p_f),
        .crd_valid(a_cv), .crd_vc(a_cvc), .crd_n(a_cn),
        .m_tvalid(m_tvalid), .m_tready(m_tready), .m_tdata(m_tdata), .m_tlast(m_tlast), .m_tdest(m_tdest)
    );

    // source: per tdest a sequence; packets of 1..4 beats; one packet at a time
    reg [31:0] seq_tx [0:VC-1];
    reg [31:0] seq_rx [0:VC-1];
    reg [1:0]  left;
    reg        quiet;
    reg        in_pkt;
    reg [VCW-1:0] cur;
    integer k;
    always @(posedge clk) begin
        if (rst) begin
            s_tvalid <= 1'b0; s_tlast <= 1'b0; s_tdest <= 0; quiet <= 1'b0; in_pkt <= 1'b0; left <= 0;
            m_tready <= 1'b0;
            for (k = 0; k < VC; k = k + 1) begin seq_tx[k] <= 0; seq_rx[k] <= 0; end
        end
        else begin
            m_tready <= ($urandom % 3 != 0);
            if (s_tvalid && s_tready) begin
                seq_tx[s_tdest] <= seq_tx[s_tdest] + 1;
                if (s_tlast) begin
                    in_pkt   <= 1'b0;
                    s_tvalid <= 1'b0;
                end
                else begin
                    left    <= left - 2'd1;
                    s_tlast <= (left == 2'd1);
                    s_tdata <= {s_tdest, 31'd0} | (seq_tx[s_tdest] + 32'd1);
                end
            end
            else if (!s_tvalid && !quiet && ($urandom % 3 != 0)) begin
                cur      <= $urandom % VC;
                left     <= $urandom % 4;
            end
            if (!s_tvalid && !in_pkt && !quiet && ($urandom % 2 == 0)) begin
                in_pkt   <= 1'b1;
                s_tvalid <= 1'b1;
                s_tdest  <= cur;
                s_tlast  <= (left == 2'd0);
                s_tdata  <= {cur, 31'd0} | seq_tx[cur];
            end
            if (m_tvalid && m_tready) begin
                if (m_tdata !== ({m_tdest, 31'd0} | seq_rx[m_tdest])) begin
                    $display("%0t ERROR tdest %0d: got %h, expected seq %0d", $time, m_tdest, m_tdata, seq_rx[m_tdest]);
                    errors = errors + 1;
                end
                seq_rx[m_tdest] <= seq_rx[m_tdest] + 1;
            end
        end
    end

    integer j;
    initial begin
        repeat (5) @(posedge clk);
        rst <= 1'b0;
        repeat (5000) @(posedge clk);
        quiet <= 1'b1;
        repeat (400) @(posedge clk);
        for (j = 0; j < VC; j = j + 1) begin
            if (seq_rx[j] != seq_tx[j]) begin
                $display("ERROR tdest %0d: %0d beats sent, %0d received", j, seq_tx[j], seq_rx[j]);
                errors = errors + 1;
            end
        end
        $display("kts_axis_tb: %0d/%0d beats (tdest 0/1) through 2 x %0d stages", seq_tx[0], seq_tx[1], NPIPE);
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
