// xcvu13p_rst_tree -- one reset, delivered to four dies as four registered
// copies. One flop fanning a reset into four SLRs (5,703 loads) measured
// -6.053 ns at synthesis on multimesh_v8; a die crossing is one TX register
// with exactly one load, its RX register (a Laguna pair -- USER_SLL_REG is
// ignored on a net with more loads, UG949), so die i gets its copy through a
// sending register pinned with the source, a landing register and a fan-out
// register pinned to die i. All four copies release on the same edge.
//
// Device-specific by name and kept beside the block-design wrapper: the
// framework's RTL knows nothing about dies.

`default_nettype none

module xcvu13p_rst_tree (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_RESET rstn_in:rstn_o0:rstn_o1:rstn_o2:rstn_o3" *)
    input  wire clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rstn_in RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire rstn_in,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rstn_o0 RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    output wire rstn_o0,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rstn_o1 RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    output wire rstn_o1,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rstn_o2 RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    output wire rstn_o2,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rstn_o3 RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    output wire rstn_o3
);
    // The four sending registers hold the same value: dont_touch keeps
    // synthesis from merging them back into the one flop this module exists
    // to avoid. Power-up 0 = reset asserted until rstn_in says otherwise.
    (* dont_touch = "true" *) reg [3:0] q    = 4'b0000;
    (* dont_touch = "true" *) reg [3:0] land = 4'b0000;
    (* dont_touch = "true" *) reg [3:0] fan  = 4'b0000;
    always @(posedge clk) begin
        q    <= {4{rstn_in}};
        land <= q;
        fan  <= land;
    end
    assign rstn_o0 = fan[0];
    assign rstn_o1 = fan[1];
    assign rstn_o2 = fan[2];
    assign rstn_o3 = fan[3];
endmodule

`default_nettype wire
