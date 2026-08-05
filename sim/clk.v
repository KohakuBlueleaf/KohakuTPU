module clk_wiz_0(
    input clk_in1,
    input reset,
    output clk_out1,
    output locked
);
    // placeholder for clock wizard, bypassed in simulation
    assign clk_out1 = clk_in1;
    assign locked = 1'b0;
endmodule