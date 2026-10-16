// The matmul 2x and its 1x as a synchronous pair: BUFGCE_DIV /1 and /2 on the
// SAME unbuffered MMCM output (clk_wiz CLKOUTn_DRIVES No_buffer), one
// CLOCK_DELAY_GROUP (UG949). Fed from the 2x's own global net instead, the 1x
// arrived 2.483 ns late against the 2x on v9t3.

`default_nettype none

module ktpu_clk_pair (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_in CLK" *)
    input  wire clk_in,
    input  wire clr,
    // FREQ_HZ or the BD calls the output clock interfaces incomplete (19-4751).
    // The block design overrides them; these are only the defaults.
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk2x CLK" *)
    (* X_INTERFACE_PARAMETER = "FREQ_HZ 600000000" *)
    output wire clk2x,
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk1x CLK" *)
    (* X_INTERFACE_PARAMETER = "FREQ_HZ 300000000" *)
    output wire clk1x
);
`ifdef SYNTHESIS
    BUFGCE_DIV #(
        .BUFGCE_DIVIDE(1),
        .IS_CE_INVERTED(1'b0), .IS_CLR_INVERTED(1'b0), .IS_I_INVERTED(1'b0)
    ) u_div1 (
        .I(clk_in), .CE(1'b1), .CLR(clr), .O(clk2x)
    );
    BUFGCE_DIV #(
        .BUFGCE_DIVIDE(2),
        .IS_CE_INVERTED(1'b0), .IS_CLR_INVERTED(1'b0), .IS_I_INVERTED(1'b0)
    ) u_div2 (
        .I(clk_in), .CE(1'b1), .CLR(clr), .O(clk1x)
    );
`else
    // Matches ktpu_div2's model: O rises WITH an I edge, never a delta after it,
    // and both outputs are the same continuous-assign depth from clk_in.
    reg ph;
    initial ph = 1'b0;
    always @(negedge clk_in) begin
        ph <= clr ? 1'b0 : ~ph;
    end
    assign clk2x = clk_in & 1'b1;
    assign clk1x = clk_in & ph;
`endif

endmodule

`default_nettype wire
