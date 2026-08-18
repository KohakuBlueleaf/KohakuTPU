// The mesh fabric's 1x, divided from the SAME 2x the pumped CUs take.

// A BUFGCE_DIV starts on whichever edge CLR released it on: a 300 MHz MMCM
// output can sit a 2x cycle out of phase with the CUs' own dividers.

`default_nettype none

// A BD elaborates the `else` branch, and a gated net is no clock: without these
// the domain reads empty and every SmartConnect fed by clk1x fails to elaborate.
module ktpu_div2 (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk2x CLK" *)
    input  wire clk2x,
    input  wire clr,
    // FREQ_HZ or the BD calls the output clock interface incomplete (19-4751).
    // The block design overrides it; this is only the default.
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk1x CLK" *)
    (* X_INTERFACE_PARAMETER = "FREQ_HZ 300000000" *)
    output wire clk1x
);
`ifdef SYNTHESIS
    BUFGCE_DIV #(
        .BUFGCE_DIVIDE(2),
        .IS_CE_INVERTED(1'b0), .IS_CLR_INVERTED(1'b0), .IS_I_INVERTED(1'b0)
    ) u_div (
        .I(clk2x), .CE(1'b1), .CLR(clr), .O(clk1x)
    );
`else
    // Matches the primitive: O rises WITH an I edge, never a delta after it.
    reg ph;
    initial ph = 1'b0;
    always @(negedge clk2x) begin
        ph <= clr ? 1'b0 : ~ph;
    end
    assign clk1x = clk2x & ph;
`endif

endmodule

`default_nettype wire
