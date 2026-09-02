// One interlink hop as the block design builds it: a kts_pipe half in each die
// with a kts_cdc between them (ASYNC 1), against the same hop on one clock
// (ASYNC 0). Three links at once -- slow->fast, fast->slow, and the synchronous
// control -- with the receiving die held in reset LONG after the sending die,
// which is what independent per-die resets do and what would lose flits if the
// crossing dropped writes instead of buffering them.
//
// Per-VC order and content are checked on every flit, and every link must drain.

`timescale 1ns / 1ps
`default_nettype none

`ifndef TB_CRED
`define TB_CRED 16
`endif
`ifndef TB_MEM
`define TB_MEM "block"
`endif

module kts_pipe_bd_tb;
    localparam integer W    = 64;
    localparam integer VC   = 2;
    localparam integer D    = `TB_CRED;
    localparam integer CN_W = 4;
    localparam integer VCW  = 1;
    localparam integer NL   = 3;

    reg fast = 1'b0;
    reg slow = 1'b0;
    always begin  // 500 MHz
        #1.0 fast = ~fast;
    end
    always begin  // 166 MHz
        #3.0 slow = ~slow;
    end
    reg rstn_a = 1'b0;                 // the sending dies
    reg rstn_b = 1'b0;                 // the landing dies, released much later

    integer errors = 0;

    genvar s;
    generate
    for (s = 0; s < NL; s = s + 1) begin : g_s
        // 0: send slow, receive fast.  1: send fast, receive slow.  2: one clock.
        localparam integer ASY = (s == 2) ? 0 : 1;
        wire a_clk = (s == 0) ? slow : fast;
        wire b_clk = (s == 1) ? slow : ((s == 2) ? a_clk : fast);
        wire a_rst = !rstn_a;
        wire b_rst = !rstn_b;

        reg  [VC-1:0]     req_valid;
        reg  [VC*W-1:0]   req_flit;
        wire [VC-1:0]     req_take;
        reg  [31:0]       seq_tx [0:VC-1];
        reg  [31:0]       seq_rx [0:VC-1];
        reg               quiet, burst;
        integer           got;

        wire              tx_valid, tx_last, c_valid, c_last, crd_valid, c_crd_valid;
        wire [VCW-1:0]    tx_vc, c_vc, crd_vc, c_crd_vc;
        wire [W-1:0]      tx_flit, c_flit;
        wire [CN_W-1:0]   crd_n, c_crd_n;
        wire [VC-1:0]     out_valid, out_last;
        wire [VC*W-1:0]   out_flit;
        reg  [VC-1:0]     out_pop;
        wire [VC*($clog2(D)+1)-1:0] credits;

        kts_tx #(.W(W), .VC(VC), .CMAX(D), .CN_W(CN_W)) u_tx (
            .clk(a_clk), .rst(a_rst),
            .req_valid(req_valid), .req_last({VC{1'b1}}), .req_flit(req_flit),
            .req_take(req_take),
            .tx_valid(tx_valid), .tx_vc(tx_vc), .tx_last(tx_last), .tx_flit(tx_flit),
            .crd_valid(c_crd_valid), .crd_vc(c_crd_vc), .crd_n(c_crd_n),
            .credits(credits)
        );
        kts_pipe_bd #(.W(W), .VCW(VCW), .CN_W(CN_W), .ASYNC(ASY), .CRED(D),
                      .MEM(`TB_MEM)) u_hop (
            .clk(a_clk), .clk_rx(b_clk), .rstn_tx(rstn_a), .rstn_rx(rstn_b),
            .i_valid(tx_valid), .i_vc(tx_vc), .i_last(tx_last), .i_flit(tx_flit),
            .o_valid(c_valid), .o_vc(c_vc), .o_last(c_last), .o_flit(c_flit),
            .i_crd_valid(crd_valid), .i_crd_vc(crd_vc), .i_crd_n(crd_n),
            .o_crd_valid(c_crd_valid), .o_crd_vc(c_crd_vc), .o_crd_n(c_crd_n)
        );
        kts_rx #(.W(W), .VC(VC), .D(D), .CN_W(CN_W)) u_rx (
            .clk(b_clk), .rst(b_rst),
            .rx_valid(c_valid), .rx_vc(c_vc), .rx_last(c_last), .rx_flit(c_flit),
            .out_valid(out_valid), .out_last(out_last), .out_flit(out_flit),
            .out_pop(out_pop),
            .crd_valid(crd_valid), .crd_vc(crd_vc), .crd_n(crd_n)
        );

        integer k;
        always @(posedge a_clk) begin
            if (a_rst) begin
                req_valid <= {VC{1'b0}};
                quiet     <= 1'b0;
                burst     <= 1'b0;
                for (k = 0; k < VC; k = k + 1) begin
                    seq_tx[k] <= 0;
                    req_flit[k*W +: W] <= {k[31:0], 32'd0};
                end
            end
            else begin
                for (k = 0; k < VC; k = k + 1) begin
                    if (req_take[k]) begin
                        seq_tx[k] <= seq_tx[k] + 1;
                        req_flit[k*W +: W] <= {k[31:0], seq_tx[k] + 32'd1};
                        req_valid[k] <= !quiet && (burst || ($urandom % 3 != 0));
                    end
                    else if (!req_valid[k]) begin
                        req_valid[k] <= !quiet && (burst || ($urandom % 3 != 0));
                    end
                end
            end
        end
        always @(posedge b_clk) begin
            if (b_rst) begin
                out_pop <= {VC{1'b0}};
                got     <= 0;
                for (k = 0; k < VC; k = k + 1) begin
                    seq_rx[k] <= 0;
                end
            end
            else begin
                for (k = 0; k < VC; k = k + 1) begin
                    out_pop[k] <= burst || ($urandom % 3 != 0);
                    if (out_pop[k] && out_valid[k]) begin
                        if (out_flit[k*W +: W] !== {k[31:0], seq_rx[k]}) begin
                            $display("%0t ERROR link %0d VC %0d: got %h, expected seq %0d",
                                     $time, s, k, out_flit[k*W +: W], seq_rx[k]);
                            errors = errors + 1;
                        end
                        seq_rx[k] <= seq_rx[k] + 1;
                        if (burst) begin
                            got <= got + 1;
                        end
                    end
                end
            end
        end
    end
    endgenerate

    // xsim will not index a generate block with a variable, so the link index
    // reaches the hierarchy only as a literal.
`define L_RUN(i, b, q) g_s[i].burst <= b ; g_s[i].quiet <= q
`define L_DRAIN(i) \
    for (j = 0; j < VC; j = j + 1) begin \
        checks = checks + 1 + g_s[i].seq_rx[j]; \
        if (g_s[i].seq_rx[j] != g_s[i].seq_tx[j]) begin \
            $display("ERROR link %0d VC %0d did not drain: tx %0d rx %0d", \
                     i, j, g_s[i].seq_tx[j], g_s[i].seq_rx[j]); \
            errors = errors + 1; \
        end \
        if (g_s[i].seq_tx[j] < 1000) begin \
            $display("ERROR link %0d VC %0d only sent %0d flits", i, j, g_s[i].seq_tx[j]); \
            errors = errors + 1; \
        end \
    end

    integer j, checks;
    real rate1;
    initial begin
        checks = 0;
        #40;
        // The sending die leaves reset 2,000 ns before the landing die: what an
        // independent per-die proc_sys_reset does.
        @(posedge slow) rstn_a <= 1'b1;
        #2000;
        @(posedge slow) rstn_b <= 1'b1;
        #20000;
        `L_RUN(0, 1'b1, 1'b0); `L_RUN(1, 1'b1, 1'b0); `L_RUN(2, 1'b1, 1'b0);
        #12000;                                  // 2,000 slow cycles
        `L_RUN(0, 1'b0, 1'b1); `L_RUN(1, 1'b0, 1'b1); `L_RUN(2, 1'b0, 1'b1);
        #8000;
        `L_DRAIN(0)
        `L_DRAIN(1)
        `L_DRAIN(2)
        // Link 1's receiver is the slow side and pops one per cycle, so 2,000
        // slow cycles must deliver close to 2,000 flits.
        rate1 = (1.0 * g_s[1].got) / 2000.0;
        $display("@@@ kts_pipe_bd_tb CRED %0d MEM %s: slow->fast %0d, fast->slow %0d (%.3f per receiver cycle), sync %0d",
                 D, `TB_MEM, g_s[0].got, g_s[1].got, rate1, g_s[2].got);
        checks = checks + 1;
        if (rate1 < 0.9) begin
            $display("ERROR link 1 delivered %.3f flits per receiver cycle, under 0.9", rate1);
            errors = errors + 1;
        end
        $display("@@@ %0d checks", checks);
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
