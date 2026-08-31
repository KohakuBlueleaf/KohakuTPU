// The elastic bench: three surfaces of the same link, differing only in how
// many register stages sit on the wire (0, 4 and 32), each with 2 VCs, D=16,
// random offers on the sending side and random pops on the receiving side.
// Every flit carries {vc, sequence}; the checker demands every flit of every
// VC in order, none lost, none duplicated, on every surface -- and measures
// each surface's throughput against min(1, D / RTT).

`timescale 1ns / 1ps
`default_nettype none

module kts_link_tb;
    localparam integer W  = 64;
    localparam integer VC = 2;
    localparam integer D  = 16;
    localparam integer CN_W = 4;
    localparam integer VCW = 1;
    localparam integer NS = 3;
    localparam integer N0 = 0, N1 = 4, N2 = 32;

    function integer pipe_n;
        input integer s;
        begin
            pipe_n = (s == 0) ? N0 : (s == 1) ? N1 : N2;
        end
    endfunction

    reg clk = 1'b0;
    reg rst = 1'b1;
    always begin
        #1.667 clk = ~clk;
    end

    integer errors = 0;
    integer cyc = 0;
    always @(posedge clk) begin
        cyc <= cyc + 1;
    end

    // ---- the three surfaces ---------------------------------------------------
    genvar s, v;
    generate
    for (s = 0; s < NS; s = s + 1) begin : g_s
        // sending side: one offer per VC, sequence numbers
        reg  [VC-1:0]     req_valid;
        reg  [VC*W-1:0]   req_flit;
        wire [VC-1:0]     req_take;
        reg  [31:0]       seq_tx [0:VC-1];
        reg  [31:0]       seq_rx [0:VC-1];
        reg               burst;               // 1: offer every cycle, pop every cycle
        reg               quiet;               // 1: offer nothing more (drain)

        wire              tx_valid, p_valid, p_crd_valid, crd_valid;
        wire [VCW-1:0]    tx_vc, p_vc, p_crd_vc, crd_vc;
        wire              tx_last, p_last;
        wire [W-1:0]      tx_flit, p_flit;
        wire [CN_W-1:0]   p_crd_n, crd_n;
        wire [VC-1:0]     out_valid, out_last;
        wire [VC*W-1:0]   out_flit;
        reg  [VC-1:0]     out_pop;
        wire [VC*($clog2(D)+1)-1:0] credits;   // CMAX = D here

        kts_tx #(.W(W), .VC(VC), .CMAX(D), .CN_W(CN_W)) u_tx (
            .clk(clk), .rst(rst),
            .req_valid(req_valid), .req_last({VC{1'b1}}), .req_flit(req_flit),
            .req_take(req_take),
            .tx_valid(tx_valid), .tx_vc(tx_vc), .tx_last(tx_last), .tx_flit(tx_flit),
            .crd_valid(p_crd_valid), .crd_vc(p_crd_vc), .crd_n(p_crd_n),
            .credits(credits)
        );
        kts_pipe #(.W(W), .VCW(VCW), .CN_W(CN_W), .N((s == 0) ? N0 : (s == 1) ? N1 : N2)) u_pipe (
            .clk(clk), .rst(rst),
            .i_valid(tx_valid), .i_vc(tx_vc), .i_last(tx_last), .i_flit(tx_flit),
            .o_valid(p_valid), .o_vc(p_vc), .o_last(p_last), .o_flit(p_flit),
            .i_crd_valid(crd_valid), .i_crd_vc(crd_vc), .i_crd_n(crd_n),
            .o_crd_valid(p_crd_valid), .o_crd_vc(p_crd_vc), .o_crd_n(p_crd_n)
        );
        kts_rx #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .CRD_BATCH(4), .TIMEOUT(16)) u_rx (
            .clk(clk), .rst(rst),
            .rx_valid(p_valid), .rx_vc(p_vc), .rx_last(p_last), .rx_flit(p_flit),
            .out_valid(out_valid), .out_last(out_last), .out_flit(out_flit),
            .out_pop(out_pop),
            .crd_valid(crd_valid), .crd_vc(crd_vc), .crd_n(crd_n)
        );

        integer k;
        integer got;                        // flits delivered while `burst`
        always @(posedge clk) begin
            if (rst) begin
                req_valid <= {VC{1'b0}};
                out_pop   <= {VC{1'b0}};
                burst     <= 1'b0;
                quiet     <= 1'b0;
                got       <= 0;
                for (k = 0; k < VC; k = k + 1) begin
                    seq_tx[k] <= 0;
                    seq_rx[k] <= 0;
                    req_flit[k*W +: W] <= {k[31:0], 32'd0};
                end
            end
            else begin
                for (k = 0; k < VC; k = k + 1) begin
                    // offer the next flit once the current one went
                    if (req_take[k]) begin
                        seq_tx[k] <= seq_tx[k] + 1;
                        req_flit[k*W +: W] <= {k[31:0], seq_tx[k] + 32'd1};
                        req_valid[k] <= !quiet && (burst || ($urandom % 4 != 0));
                    end
                    else if (!req_valid[k]) begin
                        req_valid[k] <= !quiet && (burst || ($urandom % 4 != 0));
                    end
                    // pop at random, or every cycle in the burst window
                    out_pop[k] <= burst || ($urandom % 3 != 0);
                    if (out_pop[k] && out_valid[k]) begin
                        if (out_flit[k*W +: W] !== {k[31:0], seq_rx[k]}) begin
                            $display("%0t ERROR surface %0d VC %0d: got %h, expected seq %0d",
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

    // ---- schedule -------------------------------------------------------------
    // Phase 1: random offers / random pops for 6,000 cycles. Phase 2: every VC
    // offers every cycle and pops every cycle for 4,000 cycles -- the throughput
    // window. Phase 3: drain.
    localparam integer T_RAND  = 6000;
    localparam integer T_BURST = 4000;
    integer i;
    real    rate;
    real    bound;
    integer rtt;
    initial begin
        repeat (5) @(posedge clk);
        rst <= 1'b0;
        repeat (T_RAND) @(posedge clk);
        for (i = 0; i < NS; i = i + 1) begin
            case (i)
                0: g_s[0].burst <= 1'b1;
                1: g_s[1].burst <= 1'b1;
                default: g_s[2].burst <= 1'b1;
            endcase
        end
        repeat (T_BURST) @(posedge clk);
        for (i = 0; i < NS; i = i + 1) begin
            case (i)
                0: begin g_s[0].burst <= 1'b0; g_s[0].quiet <= 1'b1; end
                1: begin g_s[1].burst <= 1'b0; g_s[1].quiet <= 1'b1; end
                default: begin g_s[2].burst <= 1'b0; g_s[2].quiet <= 1'b1; end
            endcase
        end
        repeat (2000) @(posedge clk);

        // Round trip in cycles: TX register (1) + N forward + RX landing (1) +
        // FIFO (1) + pop-to-credit register (1) + N backward + batch wait (3).
        for (i = 0; i < NS; i = i + 1) begin
            rtt   = 2 * pipe_n(i) + 7;
            // both VCs share the wire, each with D credits
            bound = (VC * D >= rtt) ? 1.0 : (1.0 * VC * D) / rtt;
            case (i)
                0: rate = (1.0 * g_s[0].got) / T_BURST;
                1: rate = (1.0 * g_s[1].got) / T_BURST;
                default: rate = (1.0 * g_s[2].got) / T_BURST;
            endcase
            $display("surface %0d: pipe %0d, %0d flits in %0d cycles = %.3f flit/cycle, bound min(1, VC*D/RTT) = %.3f",
                     i, pipe_n(i), (i == 0) ? g_s[0].got : (i == 1) ? g_s[1].got : g_s[2].got,
                     T_BURST, rate, bound);
            if (rate < 0.9 * bound) begin
                $display("%0t ERROR surface %0d: %.3f flit/cycle is under 90%% of the credit bound %.3f",
                         $time, i, rate, bound);
                errors = errors + 1;
            end
        end
        for (i = 0; i < NS; i = i + 1) begin
            case (i)
                0: if ((g_s[0].seq_rx[0] != g_s[0].seq_tx[0]) || (g_s[0].seq_rx[1] != g_s[0].seq_tx[1])) begin
                       $display("ERROR surface 0 did not drain: tx %0d/%0d rx %0d/%0d",
                                g_s[0].seq_tx[0], g_s[0].seq_tx[1], g_s[0].seq_rx[0], g_s[0].seq_rx[1]);
                       errors = errors + 1;
                   end
                1: if ((g_s[1].seq_rx[0] != g_s[1].seq_tx[0]) || (g_s[1].seq_rx[1] != g_s[1].seq_tx[1])) begin
                       $display("ERROR surface 1 did not drain: tx %0d/%0d rx %0d/%0d",
                                g_s[1].seq_tx[0], g_s[1].seq_tx[1], g_s[1].seq_rx[0], g_s[1].seq_rx[1]);
                       errors = errors + 1;
                   end
                default: if ((g_s[2].seq_rx[0] != g_s[2].seq_tx[0]) || (g_s[2].seq_rx[1] != g_s[2].seq_tx[1])) begin
                       $display("ERROR surface 2 did not drain: tx %0d/%0d rx %0d/%0d",
                                g_s[2].seq_tx[0], g_s[2].seq_tx[1], g_s[2].seq_rx[0], g_s[2].seq_rx[1]);
                       errors = errors + 1;
                   end
            endcase
        end
        $display("kts_link_tb: surface 0 tx %0d/%0d, surface 1 tx %0d/%0d, surface 2 tx %0d/%0d flits (VC0/VC1)",
                 g_s[0].seq_tx[0], g_s[0].seq_tx[1], g_s[1].seq_tx[0], g_s[1].seq_tx[1],
                 g_s[2].seq_tx[0], g_s[2].seq_tx[1]);
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
