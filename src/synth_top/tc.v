module tensorcore_synth_fate_top(
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

    wire [255:0] a, b, c;
    xorshift256 xor256_inst (
        .clk(clk_600m),
        .rst(rst),
        .seed(256'hdeadbeef),
        .en(1),
        .rand_out(a)
    ), 
    xor256_inst2 (
        .clk(clk_600m),
        .rst(rst),
        .seed(256'h23456789), //different seed
        .en(1),
        .rand_out(b)
    ), 
    xor256_inst3 (
        .clk(clk_600m),
        .rst(rst),
        .seed(256'h12345678), //different seed
        .en(1),
        .rand_out(c)
    );
    reg e5m2mode;
    reg in_valid;
    wire out_valid;
    wire [15:0] d[0:3][0:3];
    wire [255:0] dout;
    wire [15:0] test;

    genvar i, j;
    generate
        for (i = 0; i < 4; i = i + 1) begin : unpack_d
            for (j = 0; j < 4; j = j + 1) begin : unpack_d
                assign d[i][j] = dout[16*(i*4+j)+15:16*(i*4+j)];
            end
        end
    endgenerate

    assign test = d[0][0]^d[0][1]^d[0][2]^d[0][3]^d[1][0]^d[1][1]^d[1][2]^d[1][3]^d[2][0]^d[2][1]^d[2][2]^d[2][3]^d[3][0]^d[3][1]^d[3][2]^d[3][3];
    assign led_tri_io = test[7:0];

    tensorcore tensorcore_inst (
        .clk(clk_600m),
        .rst(rst),
        .e5m2mode(e5m2mode),
        .in_valid(in_valid),
        .out_valid(out_valid),
        .a_in(a),
        .b_in(b),
        .c_in(c),
        .d_out(dout)
    );
    always @(posedge clk_600m) begin
        if (rst) begin
            e5m2mode <= 0;
            in_valid <= 0;
        end else begin
            in_valid <= 1;
            e5m2mode <= ~e5m2mode;
        end
    end
endmodule