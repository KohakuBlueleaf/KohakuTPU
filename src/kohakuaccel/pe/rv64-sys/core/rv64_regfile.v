// SysCore's architectural register file: 31 x 64, two reads and one write.
//
// BLOCK RAM BY DEFAULT, AND THAT IS A LUT DECISION. At 32 deep the array is
// 2 Kbit, which a RAMB18 wastes almost entirely -- but LUT is the objective on
// this part and BRAM is not, and distributed costs ~64 LUT per read port for
// the same storage. `MEM_PRIM` is a parameter so the sweep can price both
// rather than the choice resting on this comment.
//
// TWO ARRAYS, ONE WRITE. A simple dual-port RAM has one read port, so two reads
// means two copies written identically. That is the standard trade and it is
// why the storage doubles while the LUT count does not.
//
// READ LATENCY IS 1 AND THE PIPELINE IS BUILT AROUND IT. `rd_data` is valid the
// cycle AFTER `rd_addr`. A write and a read of the same register in the same
// cycle returns the OLD value -- the forwarding network is expected to cover
// it, exactly as it covers a result that has not reached writeback.

`default_nettype none

module rv64_regfile #(
    parameter MEM_PRIM = "block"        // "block" | "distributed" | "ultra"
)(
    input  wire        clk,

    input  wire        wr_en,
    input  wire [4:0]  wr_addr,
    input  wire [63:0] wr_data,

    input  wire [4:0]  rs1_addr,
    input  wire [4:0]  rs2_addr,
    output wire [63:0] rs1_data,
    output wire [63:0] rs2_data
);
    // x0 IS NOT STORED. Refusing the write costs one AND and removes the case
    // where a stale x0 can exist at all; the read side then only has to select.
    wire we = wr_en && (wr_addr != 5'd0);

    wire [63:0] q1, q2;

    kohaku_sdpram #(
        .WIDTH(64), .DEPTH(32), .MEM_PRIM(MEM_PRIM), .READ_LAT(1)
    ) u_bank1 (
        .clk(clk),
        .wr_en(we), .wr_addr(wr_addr), .wr_data(wr_data),
        .rd_en(1'b1), .rd_addr(rs1_addr), .rd_data(q1)
    );

    kohaku_sdpram #(
        .WIDTH(64), .DEPTH(32), .MEM_PRIM(MEM_PRIM), .READ_LAT(1)
    ) u_bank2 (
        .clk(clk),
        .wr_en(we), .wr_addr(wr_addr), .wr_data(wr_data),
        .rd_en(1'b1), .rd_addr(rs2_addr), .rd_data(q2)
    );

    // The zero select is on the REGISTERED address, because the data it
    // qualifies arrives a cycle after the address that asked for it.
    reg z1, z2;
    always @(posedge clk) begin
        z1 <= (rs1_addr == 5'd0);
        z2 <= (rs2_addr == 5'd0);
    end

    assign rs1_data = z1 ? 64'd0 : q1;
    assign rs2_data = z2 ? 64'd0 : q2;

endmodule

`default_nettype wire
