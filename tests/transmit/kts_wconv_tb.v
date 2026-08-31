// Width conversion both ways: a 96-bit surface narrowed to 32 and widened
// back to 96 through two converters in series, packets of random length with
// `last`, per-VC order and content checked at the far end, and the byte-level
// contract: what a wide flit carried is what the wide flit that arrives
// carries, padding included.

`timescale 1ns / 1ps
`default_nettype none

module kts_wconv_tb;
    localparam integer WI = 96;
    localparam integer WN = 32;
    localparam integer VC = 2;
    localparam integer D  = 16;
    localparam integer CN_W = 4;
    localparam integer VCW = 1;

    reg clk = 1'b0;
    reg rst = 1'b1;
    always begin
        #1.667 clk = ~clk;
    end

    integer errors = 0;

    // ---- source: one offer per VC, packets of 1..5 flits -------------------
    reg  [VC-1:0]     req_valid, req_last;
    reg  [VC*WI-1:0]  req_flit;
    wire [VC-1:0]     req_take;
    reg  [31:0]       seq_tx [0:VC-1];
    reg  [2:0]        left   [0:VC-1];       // flits left in the packet
    reg               quiet;

    wire              a_valid, a_last, a_crd_valid;
    wire [VCW-1:0]    a_vc, a_crd_vc;
    wire [WI-1:0]     a_flit;
    wire [CN_W-1:0]   a_crd_n;
    wire [VC*($clog2(D)+1)-1:0] credits;

    kts_tx #(.W(WI), .VC(VC), .CMAX(D), .CN_W(CN_W)) u_tx (
        .clk(clk), .rst(rst),
        .req_valid(req_valid), .req_last(req_last), .req_flit(req_flit),
        .req_take(req_take),
        .tx_valid(a_valid), .tx_vc(a_vc), .tx_last(a_last), .tx_flit(a_flit),
        .crd_valid(a_crd_valid), .crd_vc(a_crd_vc), .crd_n(a_crd_n),
        .credits(credits)
    );

    // ---- wide -> narrow -> wide ---------------------------------------------
    wire              n_valid, n_last, n_crd_valid;
    wire [VCW-1:0]    n_vc, n_crd_vc;
    wire [WN-1:0]     n_flit;
    wire [CN_W-1:0]   n_crd_n;
    kts_wconv #(.WI(WI), .WO(WN), .VC(VC), .D(D), .CMAX(D), .CN_W(CN_W)) u_down (
        .clk(clk), .rst(rst),
        .i_valid(a_valid), .i_vc(a_vc), .i_last(a_last), .i_flit(a_flit),
        .i_crd_valid(a_crd_valid), .i_crd_vc(a_crd_vc), .i_crd_n(a_crd_n),
        .o_valid(n_valid), .o_vc(n_vc), .o_last(n_last), .o_flit(n_flit),
        .o_crd_valid(n_crd_valid), .o_crd_vc(n_crd_vc), .o_crd_n(n_crd_n)
    );
    wire              w_valid, w_last, w_crd_valid;
    wire [VCW-1:0]    w_vc, w_crd_vc;
    wire [WI-1:0]     w_flit;
    wire [CN_W-1:0]   w_crd_n;
    kts_wconv #(.WI(WN), .WO(WI), .VC(VC), .D(D), .CMAX(D), .CN_W(CN_W)) u_up (
        .clk(clk), .rst(rst),
        .i_valid(n_valid), .i_vc(n_vc), .i_last(n_last), .i_flit(n_flit),
        .i_crd_valid(n_crd_valid), .i_crd_vc(n_crd_vc), .i_crd_n(n_crd_n),
        .o_valid(w_valid), .o_vc(w_vc), .o_last(w_last), .o_flit(w_flit),
        .o_crd_valid(w_crd_valid), .o_crd_vc(w_crd_vc), .o_crd_n(w_crd_n)
    );

    // ---- sink ---------------------------------------------------------------
    wire [VC-1:0]     out_valid, out_last;
    wire [VC*WI-1:0]  out_flit;
    reg  [VC-1:0]     out_pop;
    kts_rx #(.W(WI), .VC(VC), .D(D), .CN_W(CN_W)) u_rx (
        .clk(clk), .rst(rst),
        .rx_valid(w_valid), .rx_vc(w_vc), .rx_last(w_last), .rx_flit(w_flit),
        .out_valid(out_valid), .out_last(out_last), .out_flit(out_flit),
        .out_pop(out_pop),
        .crd_valid(w_crd_valid), .crd_vc(w_crd_vc), .crd_n(w_crd_n)
    );

    // The flit is {seq, ~seq, seq ^ vc}: three 32-bit lanes, so a lane put in
    // the wrong place or zeroed is visible.
    function [WI-1:0] pattern;
        input [31:0] s;
        input integer k;
        begin
            pattern = {s, ~s, s ^ k[31:0]};
        end
    endfunction

    reg [31:0] seq_rx  [0:VC-1];
    reg [2:0]  left_rx [0:VC-1];
    integer k;
    always @(posedge clk) begin
        if (rst) begin
            req_valid <= {VC{1'b0}};
            req_last  <= {VC{1'b0}};
            out_pop   <= {VC{1'b0}};
            quiet     <= 1'b0;
            for (k = 0; k < VC; k = k + 1) begin
                seq_tx[k]  <= 0;
                seq_rx[k]  <= 0;
                left[k]    <= 3'd2;
                left_rx[k] <= 3'd2;
                req_flit[k*WI +: WI] <= pattern(32'd0, k);
            end
        end
        else begin
            for (k = 0; k < VC; k = k + 1) begin
                if (req_take[k]) begin
                    seq_tx[k] <= seq_tx[k] + 1;
                    req_flit[k*WI +: WI] <= pattern(seq_tx[k] + 32'd1, k);
                    if (left[k] == 3'd1) begin
                        left[k]     <= 3'd1 + ($urandom % 5);
                        req_last[k] <= ($urandom % 5) == 0;
                    end
                    else begin
                        left[k]     <= left[k] - 3'd1;
                        req_last[k] <= (left[k] == 3'd2);
                    end
                    req_valid[k] <= !quiet && ($urandom % 3 != 0);
                end
                else if (!req_valid[k]) begin
                    req_valid[k] <= !quiet && ($urandom % 3 != 0);
                end
                out_pop[k] <= ($urandom % 3 != 0);
                if (out_pop[k] && out_valid[k]) begin
                    if (out_flit[k*WI +: WI] !== pattern(seq_rx[k], k)) begin
                        $display("%0t ERROR VC %0d: got %h, expected %h (seq %0d)",
                                 $time, k, out_flit[k*WI +: WI], pattern(seq_rx[k], k), seq_rx[k]);
                        errors = errors + 1;
                    end
                    seq_rx[k] <= seq_rx[k] + 1;
                end
            end
        end
    end

    // `last` must arrive exactly where it was sent: log each side's last
    // positions and compare at the end.
    reg [31:0] last_tx [0:VC-1][0:255];
    reg [31:0] last_rx [0:VC-1][0:255];
    integer nlt [0:VC-1];
    integer nlr [0:VC-1];
    integer j;
    initial begin
        for (j = 0; j < VC; j = j + 1) begin
            nlt[j] = 0;
            nlr[j] = 0;
        end
    end
    always @(posedge clk) if (!rst) begin
        for (j = 0; j < VC; j = j + 1) begin
            if (req_take[j] && req_last[j] && (nlt[j] < 256)) begin
                last_tx[j][nlt[j]] = seq_tx[j];
                nlt[j] = nlt[j] + 1;
            end
            if (out_pop[j] && out_valid[j] && out_last[j] && (nlr[j] < 256)) begin
                last_rx[j][nlr[j]] = seq_rx[j];
                nlr[j] = nlr[j] + 1;
            end
        end
    end

    integer m;
    initial begin
        repeat (5) @(posedge clk);
        rst <= 1'b0;
        repeat (4000) @(posedge clk);
        quiet <= 1'b1;
        repeat (600) @(posedge clk);
        for (j = 0; j < VC; j = j + 1) begin
            if (seq_rx[j] != seq_tx[j]) begin
                $display("ERROR VC %0d did not drain: tx %0d rx %0d", j, seq_tx[j], seq_rx[j]);
                errors = errors + 1;
            end
            if (nlt[j] != nlr[j]) begin
                $display("ERROR VC %0d: %0d packets sent, %0d received", j, nlt[j], nlr[j]);
                errors = errors + 1;
            end
            for (m = 0; (m < nlt[j]) && (m < nlr[j]); m = m + 1) begin
                if (last_tx[j][m] != last_rx[j][m]) begin
                    $display("ERROR VC %0d packet %0d: last at flit %0d sent, %0d received",
                             j, m, last_tx[j][m], last_rx[j][m]);
                    errors = errors + 1;
                end
            end
        end
        $display("kts_wconv_tb: %0d/%0d flits, %0d/%0d packets (VC0/VC1) through 96 -> 32 -> 96",
                 seq_tx[0], seq_tx[1], nlt[0], nlt[1]);
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
