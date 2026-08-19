// One direction of a station-to-station link, credit flow controlled.

// CREDITS, NOT READY. valid/ready costs a bubble per pipeline stage or a skid
// at every stage, and makes `ready` a long backwards path across the die.

// Here the datapath is a plain shift register with no backpressure, so PIPE is
// free to grow: depth only sets how many credits are needed to cover it.

`default_nettype none

module sb_link #(
    parameter integer W    = 640,
    parameter integer PIPE = 4,                 // stages EACH way
    parameter integer CRED = 16,                // >= 2*PIPE + slack
    parameter integer STATS = 0                 // 0 costs nothing
)(
    input  wire         clk,
    input  wire         rst,

    input  wire         i_valid,
    output wire         i_ready,
    input  wire [W-1:0] i_data,

    output wire         o_valid,
    input  wire         o_ready,
    output wire [W-1:0] o_data,

    output wire [31:0]  stat_sent,
    output wire [31:0]  stat_nocred
);
    localparam integer CW = $clog2(CRED) + 1;
    // The buffer must be at least the credit count, not equal to it: credits
    // are the throughput knob, and XPM will not build a FIFO below depth 16.
    localparam integer RXD = (CRED < 16) ? 16 : CRED;

    // A link spans the die, so its reset must not be the same net the stations
    // use. dont_touch, or Vivado merges this copy back into that one net.
    (* dont_touch = "yes" *) reg rst_q;
    always @(posedge clk) rst_q <= rst;

    // ------------------------------------------------------------ send side
    reg [CW-1:0] credit;
    wire         send = i_valid && i_ready;
    wire         ret;                           // a credit coming back

    assign i_ready = |credit;

    always @(posedge clk)
        if (rst_q)               credit <= CRED[CW-1:0];
        else if (send && !ret)   credit <= credit - 1'b1;
        else if (!send && ret)   credit <= credit + 1'b1;

    // ------------------------------------------------------- die crossing
    // No ready here: a flit only leaves against a credit, so nothing can
    // arrive that the far buffer cannot hold.
    // srl_style: left alone this shift chain infers an SRL, which puts all PIPE
    // stages in ONE site and leaves the die crossing with no pipelining at all.
    (* srl_style = "register" *) reg [W-1:0] pipe_d [0:PIPE-1];
    (* srl_style = "register" *) reg [PIPE-1:0] pipe_v;
    integer k;
    always @(posedge clk) begin
        if (rst_q) pipe_v <= {PIPE{1'b0}};
        else begin
            pipe_v[0] <= send;
            for (k = 1; k < PIPE; k = k + 1) pipe_v[k] <= pipe_v[k-1];
        end
        pipe_d[0] <= i_data;
        for (k = 1; k < PIPE; k = k + 1) pipe_d[k] <= pipe_d[k-1];
    end

    // --------------------------------------------------------- receive side
    wire rxf_full;
    sync_fifo #(.DATA_WIDTH(W), .FIFO_DEPTH(RXD)) u_rxf (
        .clk(clk), .rst(rst_q),
        .wr_en(pipe_v[PIPE-1]), .wr_data(pipe_d[PIPE-1]),
        .wr_busy(rxf_full), .wr_almost(),
        .rd_en(o_valid && o_ready), .rd_data(o_data), .rd_busy(rx_empty)
    );

    wire rx_empty;
    assign o_valid = !rx_empty;

    // Credits go home down their own pipeline, so the return path is as deep
    // as the forward one and never a combinational route across the die.
    (* srl_style = "register" *) reg [PIPE-1:0] ret_p;
    always @(posedge clk)
        if (rst_q) ret_p <= {PIPE{1'b0}};
        else begin
            ret_p[0] <= o_valid && o_ready;
            for (k = 1; k < PIPE; k = k + 1) ret_p[k] <= ret_p[k-1];
        end
    assign ret = ret_p[PIPE-1];

    // `nocred` is the whole reason CRED is a knob: it says whether the credit
    // count, not the fabric, is what is limiting this crossing.
    generate
    if (STATS) begin : g_stats
        reg [31:0] n_sent, n_nocred;
        always @(posedge clk)
            if (rst_q) begin
                n_sent   <= 32'd0;
                n_nocred <= 32'd0;
            end else begin
                if (send)                  n_sent   <= n_sent + 32'd1;
                if (i_valid && !i_ready)   n_nocred <= n_nocred + 32'd1;
            end
        assign stat_sent   = n_sent;
        assign stat_nocred = n_nocred;
    end else begin : g_nostats
        assign stat_sent   = 32'd0;
        assign stat_nocred = 32'd0;
    end
    endgenerate

`ifndef SYNTHESIS
    always @(posedge clk)
        if (!rst_q && pipe_v[PIPE-1] && rxf_full)
            $display("%0t ERROR sb_link: RX full with a flit arriving -- CRED %0d is below 2*PIPE %0d",
                     $time, CRED, 2*PIPE);
`endif
endmodule

`default_nettype wire
