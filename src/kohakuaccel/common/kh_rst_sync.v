// Async-assert / sync-release reset synchronizer — the domain-entry form the
// reset architecture mandates: only the raw reset ever crosses domains, one
// synchronizer per domain, loads see a local synchronous release. Assertion
// is immediate on arstn falling; release waits STAGES clean clk edges. The
// output is a plain FF: the placer and phys_opt may replicate it toward its
// loads.
`default_nettype none

module kh_rst_sync #(
    parameter integer STAGES = 3
)(
    input  wire clk,
    input  wire arstn,
    output wire rstn
);
    (* ASYNC_REG = "true" *) reg [STAGES-1:0] q;

    always @(posedge clk or negedge arstn) begin
        if (!arstn) q <= {STAGES{1'b0}};
        else        q <= {q[STAGES-2:0], 1'b1};
    end

    assign rstn = q[STAGES-1];

endmodule

`default_nettype wire
