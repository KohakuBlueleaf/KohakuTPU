// Quiescence clock gate: withholds an edge from a module whose next state is
// provably its present state. Exactness argument: sim/verilator/docs/models.md.
//
//   skip posedge t  iff  s_t == s_{t-1} == ... == s_{t-1-SETTLE}, `idle` throughout
//
// An ICG: the enable latch is transparent while `clk` is low and closed at the
// rise, so the decision sees the inputs the edge samples even when they come
// from a faster clock that moves between this clock's negedge and posedge, and
// `gclk` cannot glitch.

`default_nettype none

module vlt_qgate #(
    parameter integer W      = 1,
    parameter integer SETTLE = 32
)(
    input  wire         clk,
    input  wire [W-1:0] in,
    input  wire         idle,
    output wire         gclk
);
    // +define+VLT_QGATE_SETTLE=n overrides every gate: the differential benches'
    // negative control builds below each module's bound and must fail.
`ifdef VLT_QGATE_SETTLE
    localparam integer ST = `VLT_QGATE_SETTLE;
`else
    localparam integer ST = SETTLE;
`endif

    reg [W-1:0] in_q;           // the sample taken at the last posedge
    reg [7:0]   quiet = 8'd0;   // consecutive idle samples equal to the one before
    reg         run_l = 1'b1;

    wire same  = (in == in_q);
    wire run_c = !(same && idle && (quiet == ST[7:0]));

    always_latch begin
        if (!clk) run_l = run_c;
    end

    always @(posedge clk) begin
        in_q  <= in;
        quiet <= (same && idle) ? ((quiet == ST[7:0]) ? quiet : quiet + 8'd1) : 8'd0;
    end

    assign gclk = clk & run_l;

    initial begin
        if (ST < 0 || ST > 255) begin
            $display("vlt_qgate: SETTLE %0d outside 0..255", ST);
            $fatal(1);
        end
    end
endmodule

`default_nettype wire
