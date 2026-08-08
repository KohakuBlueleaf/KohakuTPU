// A D-deep pipeline delay, written as one shift register.
//
// vec_alu threads ~20 control signals across a 13-stage pipeline. Naming the
// delay keeps the stage arithmetic checkable by inspection: the instance says
// "produced at cycle 1, consumed at cycle 6" and D is the difference.
//
// NOT a memory: no address, no read port, so CLAUDE.md's rule about naming
// BRAM/URAM primitives does not apply. That rule exists because an inferred
// memory's READ LATENCY depends on a tool heuristic; a shift register's does not.

`default_nettype none

module vec_delay #(
    parameter integer W = 1,
    parameter integer D = 1
)(
    input  wire         clk,
    input  wire [W-1:0] d,
    output wire [W-1:0] q
);
    generate
    if (D == 0) begin : g_pass
        assign q = d;
    end else if (D == 1) begin : g_one
        reg [W-1:0] r;
        always @(posedge clk) r <= d;
        assign q = r;
    end else begin : g_sr
        reg [W*D-1:0] sr;
        always @(posedge clk) sr <= {sr[W*(D-1)-1:0], d};
        assign q = sr[W*D-1 -: W];
    end
    endgenerate
endmodule

`default_nettype wire
