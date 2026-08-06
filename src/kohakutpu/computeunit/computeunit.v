module Unit(
    input clk,
    input rst,
    input in_valid,
    input [255:0] a,
    input [255:0] b,
    input [255:0] c,
    input [7:0] opmode,
    output [255:0] out,
    output out_valid
);
    wire [255:0] ALU_out, tensorcore_out;
    wire ALU_out_valid, tensorcore_out_valid;
    FP16ALUArray alu_array(
        .clk(clk),
        .rst(rst),
        .in_valid(in_valid),
        .opmode(opmode[5:0]),
        .a(a),
        .b(b),
        .c(c),
        .out(ALU_out),
        .out_valid(ALU_out_valid)
    );
    tensorcore tensorcore(
        .clk(clk),
        .rst(rst),
        .in_valid(in_valid),
        .e5m2mode(opmode[6]),
        .a(a),
        .b(b),
        .c(c),
        .d(tensorcore_out),
        .out_valid(tensorcore_out_valid)
    );

    assign out = opmode[7] ? tensorcore_out : ALU_out;
    assign out_valid = opmode[7] ? tensorcore_out_valid : ALU_out_valid;
endmodule


module UnitWithBRAM(
    input clk,
    input rst,

    // BRAM1 interface, 288 width 1024 depth
    output reg [9:0] bram1_addr,
    output reg [287:0] bram1_wdata,
    input wire [287:0] bram1_rdata,
    output wire [35:0] bram1_we,

    // BRAM2 interface, 288 width 1024 depth
    output reg [9:0] bram2_addr,
    output reg [287:0] bram2_wdata,
    input wire [287:0] bram2_rdata,
    output wire [35:0] bram2_we,

    // BRAM3 interface, 288 width 1024 depth
    output reg [9:0] bram3_addr,
    output reg [287:0] bram3_wdata,
    input wire [287:0] bram3_rdata,
    output wire [35:0] bram3_we,

    // BRAM4 interface, 288 width 1024 depth
    output reg [9:0] bram4_addr,
    output reg [287:0] bram4_wdata,
    input wire [287:0] bram4_rdata,
    output reg [35:0] bram4_we
);
    wire [255:0] a, b, c, out;
    wire out_valid;
    reg [7:0] opmode;

    assign bram1_we = 36'b0;
    assign bram2_we = 36'b0;
    assign bram3_we = 36'b0;

    assign a = bram1_rdata[255:0];
    assign b = bram2_rdata[255:0];
    assign c = bram3_rdata[255:0];

    Unit unit(
        .clk(clk),
        .rst(~rst),
        .in_valid(1'b1),
        .a(a),
        .b(b),
        .c(c),
        .opmode(opmode),
        .out(out),
        .out_valid(out_valid)
    );

    always @(posedge clk or negedge rst) begin
        if(!rst) begin
            bram1_addr <= 10'b0;
            bram1_wdata <= 288'b0;
            bram2_addr <= 10'b0;
            bram2_wdata <= 288'b0;
            bram3_addr <= 10'b0;
            bram3_wdata <= 288'b0;
            bram4_addr <= 10'b0;
            bram4_wdata <= 288'b0;
            bram4_we <= 36'b0;
            opmode <= 8'b0;
        end else begin
            if(bram1_addr == 0 & !out_valid) begin
                opmode <= 8'b0;
                bram1_addr <= bram1_addr + 10'b1;
                bram2_addr <= bram2_addr + 10'b1;
                bram3_addr <= bram3_addr + 10'b1;
            end else if(bram1_addr[9] == 0) begin
                bram1_addr <= bram1_addr + 10'b1;
                bram2_addr <= bram2_addr + 10'b1;
                bram3_addr <= bram3_addr + 10'b1;
            end else if(bram1_addr == 10'b1000000000 & !out_valid) begin
                opmode <= 8'b10000000;
                bram1_addr <= bram1_addr + 10'b1;
                bram2_addr <= bram2_addr + 10'b1;
                bram3_addr <= bram3_addr + 10'b1;
            end else if(bram1_addr > 10'b1000000000) begin
                bram1_addr <= bram1_addr + 10'b1;
                bram2_addr <= bram2_addr + 10'b1;
                bram3_addr <= bram3_addr + 10'b1;
            end
            if(out_valid) begin
                bram4_addr <= bram4_addr + 10'b1;
                bram4_wdata <= {32'b0, out};
                bram4_we <= {36{1'b1}};
            end else begin
                bram4_we <= 36'b0;
            end
        end
    end
endmodule