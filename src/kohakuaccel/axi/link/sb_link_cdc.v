// One direction of a station-to-station link that ALSO crosses clock domains.
// Same credit contract as sb_link; the crossing lives here, not in the BD.

// A per-SLR fabric clock is the point: a shared 400 MHz clock over four SLRs
// couples every station to the worst one, and the SLL hop alone is 0.755 ns.

// CREDIT RETURN IS A GRAY COUNTER, not a pulse: pops outpace a slower near
// clock, and a dropped pulse starves the link silently instead of failing.

`default_nettype none

module sb_link_cdc #(
    parameter integer W    = 640,
    parameter integer PIPE = 4,                 // stages, sender side
    parameter integer CRED = 16,
    parameter         MEMORY_TYPE = "distributed"   // T3: "block" -> BRAM RX
)(
    input  wire         i_clk,
    input  wire         i_rst,
    input  wire         i_valid,
    output wire         i_ready,
    input  wire [W-1:0] i_data,

    input  wire         o_clk,
    input  wire         o_rst,
    output wire         o_valid,
    input  wire         o_ready,
    output wire [W-1:0] o_data,

    output wire [31:0]  stat_sent,
    output wire [31:0]  stat_nocred
);
    localparam integer CW  = $clog2(CRED) + 2;
    localparam integer RXD = (CRED < 16) ? 16 : CRED;

    (* dont_touch = "yes" *) reg irst_q, orst_q;
    always @(posedge i_clk) begin
        irst_q <= i_rst;
    end
    always @(posedge o_clk) begin
        orst_q <= o_rst;
    end

    wire send = i_valid && i_ready;

    // ------------------------------------------------------- sender counters
    reg [CW-1:0] sent_cnt;
    always @(posedge i_clk) begin
        if (irst_q) begin
            sent_cnt <= {CW{1'b0}};
        end
        else if (send) begin
            sent_cnt <= sent_cnt + 1'b1;
        end
    end

    // --------------------------------------------------- pops, in the far clock
    reg [CW-1:0] pop_bin, pop_gray;
    wire         pop = o_valid && o_ready;
    always @(posedge o_clk) begin
        if (orst_q) begin
            pop_bin  <= {CW{1'b0}};
            pop_gray <= {CW{1'b0}};
        end else if (pop) begin
            pop_bin  <= pop_bin + 1'b1;
            pop_gray <= ((pop_bin + 1'b1) >> 1) ^ (pop_bin + 1'b1);
        end
    end

    (* async_reg = "true" *) reg [CW-1:0] pg_s1, pg_s2;
    always @(posedge i_clk) begin
        pg_s1 <= pop_gray;
        pg_s2 <= pg_s1;
    end

    reg [CW-1:0] pop_sync;
    integer b;
    always @(*) begin
        pop_sync[CW-1] = pg_s2[CW-1];
        for (b = CW-2; b >= 0; b = b - 1) begin
            pop_sync[b] = pop_sync[b+1] ^ pg_s2[b];
        end
    end

    wire [CW-1:0] outstanding = sent_cnt - pop_sync;
    assign i_ready = (outstanding < CRED[CW-1:0]);

    // ------------------------------------------------------- die crossing
    // srl_style: left alone this infers an SRL, putting every stage in ONE
    // site and leaving the crossing with no pipelining at all.
    (* srl_style = "register" *) reg [W-1:0] pipe_d [0:PIPE-1];
    (* srl_style = "register" *) reg [PIPE-1:0] pipe_v;
    integer k;
    always @(posedge i_clk) begin
        if (irst_q) begin
            pipe_v <= {PIPE{1'b0}};
        end
        else begin
            pipe_v[0] <= send;
            for (k = 1; k < PIPE; k = k + 1) begin
                pipe_v[k] <= pipe_v[k-1];
            end
        end
        pipe_d[0] <= i_data;
        for (k = 1; k < PIPE; k = k + 1) begin
            pipe_d[k] <= pipe_d[k-1];
        end
    end

    wire rxf_full, rx_empty;
    async_fifo #(.DATA_WIDTH(W), .FIFO_DEPTH(RXD), .MEMORY_TYPE(MEMORY_TYPE)) u_rxf (
        .wr_clk(i_clk), .wr_rst(irst_q),
        .wr_en(pipe_v[PIPE-1]), .wr_data(pipe_d[PIPE-1]), .wr_full(rxf_full),
        .rd_clk(o_clk), .rd_en(pop), .rd_data(o_data), .rd_empty(rx_empty)
    );
    assign o_valid = !rx_empty;

    reg [31:0] n_sent, n_nocred;
    always @(posedge i_clk) begin
        if (irst_q) begin
            n_sent   <= 32'd0;
            n_nocred <= 32'd0;
        end else begin
            if (send) begin
                n_sent   <= n_sent + 32'd1;
            end
            if (i_valid && !i_ready) begin
                n_nocred <= n_nocred + 32'd1;
            end
        end
    end
    assign stat_sent   = n_sent;
    assign stat_nocred = n_nocred;

`ifndef SYNTHESIS
    always @(posedge i_clk) begin
        if (!irst_q && pipe_v[PIPE-1] && rxf_full) begin
            $display("%0t ERROR sb_link_cdc: RX full with a flit arriving -- CRED %0d is below 2*PIPE %0d",
                     $time, CRED, 2*PIPE);
        end
    end
`endif
endmodule

`default_nettype wire
