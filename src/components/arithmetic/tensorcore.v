module tensorcore (
    input wire clk,
    input wire rst,
    input e5m2mode,
    input in_valid,
    input wire [255:0] a_in,  // 4x8x8 bits
    input wire [255:0] b_in,  // 8x4x8 bits
    input wire [255:0] c_in,  // 4x4x16 bits
    output wire [255:0] d_out, // 4x4x16 bits
    output wire out_valid
);
    reg [11:0] mul_intermediate [0:7][0:3][0:3];
    reg mul_outvalid [0:7][0:3];
    reg [11:0] acc_intermediate [0:6][0:3][0:3];
    reg acc_outvalid [0:6][0:3];
    reg [3:0] final_out_valid;
    assign out_valid = final_out_valid == 4'b1111;

    wire [7:0] a [3:0][7:0];  // 4x8 matrix of 8-bit elements
    wire [7:0] b [7:0][3:0];  // 8x4 matrix of 8-bit elements
    wire [15:0] c [3:0][3:0]; // 4x4 matrix of 16-bit elements
    wire [15:0] d [3:0][3:0]; // 4x4 matrix of 16-bit elements

    // Unpack input arrays
    genvar i, j;
    generate
        for (i = 0; i < 4; i = i + 1) begin : unpack_a
            for (j = 0; j < 8; j = j + 1) begin : unpack_a_inner
                assign a[i][j] = a_in[i*64 + j*8 +: 8];
            end
        end
        
        for (i = 0; i < 8; i = i + 1) begin : unpack_b
            for (j = 0; j < 4; j = j + 1) begin : unpack_b_inner
                assign b[i][j] = b_in[i*32 + j*8 +: 8];
            end
        end
        
        for (i = 0; i < 4; i = i + 1) begin : unpack_c
            for (j = 0; j < 4; j = j + 1) begin : unpack_c_inner
                assign c[i][j] = c_in[i*64 + j*16 +: 16];
            end
        end
    endgenerate
    generate
        for (i = 0; i < 4; i = i + 1) begin : pack_d
            for (j = 0; j < 4; j = j + 1) begin : pack_d_inner
                assign d_out[i*64 + j*16 +: 16] = d[i][j];
            end
        end
    endgenerate


    genvar acc_j;
    generate
        for (i = 0; i < 4; i = i + 1) begin
            for (j = 0; j < 8; j = j + 1) begin
                wire [11:0] qa, qb, qc, qd;
                wire out_valid_temp;
                FP8VectorMul fp8mul(
                    .clk(clk),
                    .rst(rst),
                    .e5m2mode(e5m2mode),
                    .in_valid(in_valid),
                    .out_valid(out_valid_temp),
                    .q(a[i][j]),
                    .a(b[j][0]),
                    .b(b[j][1]),
                    .c(b[j][2]),
                    .d(b[j][3]),
                    .qa(qa),
                    .qb(qb),
                    .qc(qc),
                    .qd(qd)
                );
                always @(*) begin
                    mul_intermediate[j][i][0] = qa;
                    mul_intermediate[j][i][1] = qb;
                    mul_intermediate[j][i][2] = qc;
                    mul_intermediate[j][i][3] = qd;
                end
                always @(posedge clk) begin
                    mul_outvalid[j][i] <= out_valid_temp;
                end
            end
            for (acc_j = 0; acc_j < 4; acc_j = acc_j + 1) begin
                wire [11:0] aa, bb, cc, dd;
                wire out_valid_temp;
                FPVectorAdd #(
                    .EXP_BITS(5),
                    .MANT_BITS(6)
                ) fpadd (
                    .clk(clk),
                    .rst(rst),
                    .in_valid(mul_outvalid[2*acc_j][i] & mul_outvalid[2*acc_j+1][i]),
                    .out_valid(out_valid_temp),
                    .a_1(mul_intermediate[2*acc_j][i][0]),
                    .b_1(mul_intermediate[2*acc_j][i][1]),
                    .c_1(mul_intermediate[2*acc_j][i][2]),
                    .d_1(mul_intermediate[2*acc_j][i][3]),
                    .a_2(mul_intermediate[2*acc_j+1][i][0]),
                    .b_2(mul_intermediate[2*acc_j+1][i][1]),
                    .c_2(mul_intermediate[2*acc_j+1][i][2]),
                    .d_2(mul_intermediate[2*acc_j+1][i][3]),
                    .a_out(aa),
                    .b_out(bb),
                    .c_out(cc),
                    .d_out(dd)
                );
                always @(posedge clk) begin
                    if (out_valid_temp) begin
                        acc_intermediate[acc_j][i][0] <= aa;
                        acc_intermediate[acc_j][i][1] <= bb;
                        acc_intermediate[acc_j][i][2] <= cc;
                        acc_intermediate[acc_j][i][3] <= dd;
                        acc_outvalid[acc_j][i] <= 1;
                    end else begin
                        acc_outvalid[acc_j][i] <= 0;
                    end
                end
            end
            for (acc_j = 4; acc_j < 7; acc_j = acc_j + 1) begin
                wire [11:0] aa, bb, cc, dd;
                wire out_valid_temp;
                FPVectorAdd #(
                    .EXP_BITS(5),
                    .MANT_BITS(6)
                ) fpadd (
                    .clk(clk),
                    .rst(rst),
                    .in_valid(acc_outvalid[(acc_j-4)*2][i] & acc_outvalid[(acc_j-4)*2+1][i]),
                    .out_valid(out_valid_temp),
                    .a_1(acc_intermediate[(acc_j-4)*2][i][0]),
                    .b_1(acc_intermediate[(acc_j-4)*2][i][1]),
                    .c_1(acc_intermediate[(acc_j-4)*2][i][2]),
                    .d_1(acc_intermediate[(acc_j-4)*2][i][3]),
                    .a_2(acc_intermediate[(acc_j-4)*2+1][i][0]),
                    .b_2(acc_intermediate[(acc_j-4)*2+1][i][1]),
                    .c_2(acc_intermediate[(acc_j-4)*2+1][i][2]),
                    .d_2(acc_intermediate[(acc_j-4)*2+1][i][3]),
                    .a_out(aa),
                    .b_out(bb),
                    .c_out(cc),
                    .d_out(dd)
                );
                always @(posedge clk) begin
                    if (out_valid_temp) begin
                        acc_intermediate[acc_j][i][0] <= aa;
                        acc_intermediate[acc_j][i][1] <= bb;
                        acc_intermediate[acc_j][i][2] <= cc;
                        acc_intermediate[acc_j][i][3] <= dd;
                        acc_outvalid[acc_j][i] <= 1;
                    end else begin
                        acc_outvalid[acc_j][i] <= 0;
                    end
                end
            end
            
            wire [3:0] final_out_valid_temp;
            FPVectorAdd #(
                .EXP_BITS(5),
                .MANT_BITS(10)
            ) fp16add (
                .clk(clk),
                .rst(rst),
                .in_valid(acc_outvalid[6][i]),
                .out_valid(final_out_valid_temp[i]),
                .a_1({acc_intermediate[6][i][0], 4'b0}),
                .b_1({acc_intermediate[6][i][1], 4'b0}),
                .c_1({acc_intermediate[6][i][2], 4'b0}),
                .d_1({acc_intermediate[6][i][3], 4'b0}),
                .a_2(c[i][0]),
                .b_2(c[i][1]),
                .c_2(c[i][2]),
                .d_2(c[i][3]),
                .a_out(d[i][0]),
                .b_out(d[i][1]),
                .c_out(d[i][2]),
                .d_out(d[i][3])
            );
            always @(posedge clk) begin
                if (final_out_valid_temp[i]) begin
                    final_out_valid[i] <= 1;
                end else begin
                    final_out_valid[i] <= 0;
                end
            end
        end
    endgenerate
endmodule