// Async-assert / sync-release reset synchronizer — the domain-entry form the
// reset architecture mandates: only the raw reset ever crosses domains, one
// synchronizer per domain, loads see a local synchronous release. Assertion
// is immediate on arstn falling; release waits STAGES clean clk edges. The
// last stage is a plain FF, outside ASYNC_REG: Vivado never replicates an
// ASYNC_REG cell, so only a plain output lets the placer and phys_opt copy it
// toward its loads (one stage fed ~10k sysnode loads per die on v9t5).
`default_nettype none

module kh_rst_sync #(
    parameter integer STAGES = 3
)(
    input  wire clk,
    input  wire arstn,
    output wire rstn
);
    (* ASYNC_REG = "true" *) reg [STAGES-2:0] s;
    reg q;

    always @(posedge clk or negedge arstn) begin
        if (!arstn) begin
            s <= {(STAGES-1){1'b0}};
            q <= 1'b0;
        end
        else begin
            s <= (s << 1) | 1'b1;
            q <= s[STAGES-2];
        end
    end

    assign rstn = q;

endmodule

`default_nettype wire
