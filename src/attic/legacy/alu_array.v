module fp16alu_array_synth_fake_top(
    input clk,
    input rst,
    output [7:0] led_tri_io
);
    wire clk_600m;
    wire pll_locked;

    // Instantiate the clocking wizard
    clk_wiz_0 clock_gen (
        .clk_out1   (clk_600m),
        .locked     (pll_locked),
        .clk_in1    (clk),
        .reset      (rst)
    );

    reg [5:0] opmode;
    reg [15:0] a [0:15];
    reg [15:0] b [0:15];
    reg [15:0] c [0:15];
    reg in_valid;
    wire out_valid;
    wire [15:0] d [0:15];
    wire [15:0] test;

    assign test = d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[4] ^ d[5] ^ d[6] ^ d[7] ^ d[8] ^ d[9] ^ d[10] ^ d[11] ^ d[12] ^ d[13] ^ d[14] ^ d[15];
    assign led_tri_io = test[7:0] & test[15:8] & out_valid;

    FP16ALUArray fp16aluarray_inst (
        .clk(clk_600m),
        .rst(rst),
        .in_valid(in_valid),
        .out_valid(out_valid),
        .a(  {a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8], a[9], a[10], a[11], a[12], a[13], a[14], a[15]}),
        .b(  {b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]}),
        .c(  {c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], c[9], c[10], c[11], c[12], c[13], c[14], c[15]}),
        .out({d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7], d[8], d[9], d[10], d[11], d[12], d[13], d[14], d[15]}),
        .opmode(opmode)
    );
    integer i, j;
    always @(posedge clk_600m) begin
        if (rst) begin
            opmode <= 0;
            in_valid <= 0;
            for (i = 0; i < 16; i = i + 1) begin
                a[i] <= 0;
                b[i] <= 0;
                c[i] <= 0;
            end
        end else begin
            opmode <= opmode + 1;
            in_valid <= 1;
            for (i = 0; i < 16; i = i + 1) begin
                a[i] <= a[i] + 1;
                b[i] <= b[i] + 2;
                c[i] <= c[i] + 3;
            end
        end
    end
endmodule