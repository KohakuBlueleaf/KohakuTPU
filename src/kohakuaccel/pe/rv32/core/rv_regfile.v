// rv_regfile -- 32 x 32, two read ports, one write port, read latency 1.
//
// TWO STORAGE VARIANTS BEHIND ONE PARAMETER, and that is the point of the
// module rather than an afterthought: the register file is the largest single
// LUT item in a core this small, so whether it should be LUTRAM or block RAM is
// a number, not an opinion. MEM_PRIM selects it; docs/arch/pe/performance.md
// carries the verdict.
//
// 2R1W is built as TWO MIRRORED 1W1R arrays written identically -- the standard
// FPGA construction, because no primitive offers two independent read ports.
//
// READ LATENCY IS PIPELINE STRUCTURE. The address is captured at the end of
// decode and the data is out one cycle later, which is why this core has a
// separate operand-fetch boundary. Both variants have latency 1, so swapping
// them changes resources and nothing else.
//
// THE WRITE-THROUGH BYPASS IS NOT OPTIONAL. A write lands at the same edge that
// captures a read address four instructions behind it, and a synchronous array
// returns the pre-write value for that read. The forwarding network in rv_id
// covers distances 1..3; this covers distance 4, and without it the core is
// wrong only for that one spacing.

`default_nettype none

module rv_regfile #(
    parameter MEM_PRIM = "distributed"          // "distributed" | "block"
)(
    input  wire        clk,

    // Addresses arrive combinationally from the fetched instruction and are
    // captured here, so the array's own output register is the pipeline
    // boundary rather than a flop in front of it.
    input  wire        ra_en,
    input  wire [4:0]  ra1,
    input  wire [4:0]  ra2,
    output wire [31:0] rd1,
    output wire [31:0] rd2,

    input  wire        we,
    input  wire [4:0]  wa,
    input  wire [31:0] wd
);
    reg  [4:0] ra1_q, ra2_q;

    // The address the arrays actually see this cycle: held while the front end
    // is stalled, so the read is re-issued every cycle and a write that lands
    // mid-stall is visible one cycle later.
    wire [4:0] ra1_live = ra_en ? ra1 : ra1_q;
    wire [4:0] ra2_live = ra_en ? ra2 : ra2_q;

    always @(posedge clk) begin
        ra1_q <= ra1_live;
        ra2_q <= ra2_live;
    end

    wire wr = we && (wa != 5'd0);

    reg        byp1, byp2;
    reg [31:0] byp_d;
    always @(posedge clk) begin
        byp1  <= wr && (wa == ra1_live);
        byp2  <= wr && (wa == ra2_live);
        byp_d <= wd;
    end

    wire [31:0] q1, q2;

    kohaku_sdpram #(.WIDTH(32), .DEPTH(32), .MEM_PRIM(MEM_PRIM), .READ_LAT(1))
    u_p1 (.clk(clk), .wr_en(wr), .wr_addr(wa), .wr_data(wd),
          .rd_en(1'b1), .rd_addr(ra1_live), .rd_data(q1));

    kohaku_sdpram #(.WIDTH(32), .DEPTH(32), .MEM_PRIM(MEM_PRIM), .READ_LAT(1))
    u_p2 (.clk(clk), .wr_en(wr), .wr_addr(wa), .wr_data(wd),
          .rd_en(1'b1), .rd_addr(ra2_live), .rd_data(q2));

    // x0 is forced here rather than written as zero, so nothing depends on the
    // array having been initialised.
    assign rd1 = (ra1_q == 5'd0) ? 32'd0 : (byp1 ? byp_d : q1);
    assign rd2 = (ra2_q == 5'd0) ? 32'd0 : (byp2 ? byp_d : q2);

endmodule

`default_nettype wire
