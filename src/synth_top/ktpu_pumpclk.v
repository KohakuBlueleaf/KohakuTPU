// The pumped matmul's clock pair, from ONE source and ONE reset.

// UG949: the fast clock must pass a BUFGCE_DIV too, DIVIDE 1, or its insertion
// delay does not match the divided one and the pair carries the difference.

`default_nettype none

// Both buffers share CLR, so their outputs align on release whatever the
// divide -- UG572. Separate CLRs is what lets a pair phase-shift in hardware.
module ktpu_pumpclk (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_in CLK" *)
    input  wire clk_in,
    input  wire clr,
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk2x CLK" *)
    (* X_INTERFACE_PARAMETER = "FREQ_HZ 500000000" *)
    output wire clk2x,
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk1x CLK" *)
    (* X_INTERFACE_PARAMETER = "FREQ_HZ 250000000" *)
    output wire clk1x
);
`ifdef SYNTHESIS
    BUFGCE_DIV #(
        .BUFGCE_DIVIDE(1),
        .IS_CE_INVERTED(1'b0), .IS_CLR_INVERTED(1'b0), .IS_I_INVERTED(1'b0)
    ) u_2x (
        .I(clk_in), .CE(1'b1), .CLR(clr), .O(clk2x)
    );
    BUFGCE_DIV #(
        .BUFGCE_DIVIDE(2),
        .IS_CE_INVERTED(1'b0), .IS_CLR_INVERTED(1'b0), .IS_I_INVERTED(1'b0)
    ) u_1x (
        .I(clk_in), .CE(1'b1), .CLR(clr), .O(clk1x)
    );
`else
    // Matches the primitive: O rises WITH an I edge, never a delta after it.
    reg ph;
    initial ph = 1'b0;
    always @(negedge clk_in) begin
        ph <= clr ? 1'b0 : ~ph;
    end
    assign clk2x = clk_in;
    assign clk1x = clk_in & ph;
`endif

endmodule

`default_nettype wire
