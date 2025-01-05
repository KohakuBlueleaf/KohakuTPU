module FP8VectorMul1 (
    input clk,
    input rst,
    input e5m2mode,
    input in_valid,
    input [7:0] q,
    input [7:0] a,
    input [7:0] b,
    input [7:0] c,
    input [7:0] d,

    output [11:0] qa,
    output [11:0] qb,
    output [11:0] qc,
    output [11:0] qd,
    output reg out_valid
);
    // Input Part
    reg [7:0] a_reg1; reg [7:0] a_reg2; reg [7:0] a_reg3;
    reg [7:0] b_reg1; reg [7:0] b_reg2; reg [7:0] b_reg3;
    reg [7:0] c_reg1; reg [7:0] c_reg2; reg [7:0] c_reg3;
    reg [7:0] d_reg1; reg [7:0] d_reg2; reg [7:0] d_reg3;
    reg [7:0] q_reg1; reg [7:0] q_reg2; reg [7:0] q_reg3;

    wire [17:0] B;
    wire [47:0] C; reg [47:0] Creg1;
    wire [47:0] P;
    wire [29:0] A;
    //remember to handle subnormal numbers
    assign B = e5m2mode ? {15'b0, q[6:2]!=0, q[1:0]} : {14'b0, q[6:3]!=0, q[2:0]};
    assign A = e5m2mode 
                ? {10'b0, d[1:0], 4'b0, c[1:0], 4'b0, b[1:0], 4'b0, a[1:0]} 
                : { 3'b0, d[2:0], 5'b0, c[2:0], 5'b0, b[2:0], 5'b0, a[2:0]} ;
    assign C = e5m2mode
                ? {
                    26'b0, d[6:2]==0 ? 5'b0 : d[1:0], 
                    4'b0,  c[6:2]==0 ? 5'b0 : c[1:0], 
                    4'b0,  b[6:2]==0 ? 5'b0 : b[1:0], 
                    4'b0,  a[6:2]==0 ? 5'b0 : a[1:0], 2'b0
                }: {
                    18'b0, d[6:3]==0 ? 4'b0 : d[2:0], 
                    5'b0,  c[6:3]==0 ? 4'b0 : c[2:0], 
                    5'b0,  b[6:3]==0 ? 4'b0 : b[2:0], 
                    5'b0,  a[6:3]==0 ? 4'b0 : a[2:0], 3'b0
                };

    reg out_valid_reg1;
    reg out_valid_reg2;

    always @(posedge clk) begin
        Creg1 <= C;
        out_valid_reg1 <= in_valid;
        out_valid_reg2 <= out_valid_reg1;
        out_valid <= out_valid_reg2;
        a_reg1 <= a;
        a_reg2 <= a_reg1;
        a_reg3 <= a_reg2;
        b_reg1 <= b;
        b_reg2 <= b_reg1;
        b_reg3 <= b_reg2;
        c_reg1 <= c;
        c_reg2 <= c_reg1;
        c_reg3 <= c_reg2;
        d_reg1 <= d;
        d_reg2 <= d_reg1;
        d_reg3 <= d_reg2;
        q_reg1 <= q;
        q_reg2 <= q_reg1;
        q_reg3 <= q_reg2;
    end

    /*
        Enable input reg, pipe reg and output reg to implement pipeline for FP8VectorMul
        Since the output logic after DSP is still complex,
        we need to add a register between the DSP alu and output logic, which is the output reg here
    */
    DSP #(
        .INPUTREG(1),
        .OUTPUTREG(1),
        .DSPPIPEREG(1),
        .CONTROLREG(0),
        .NEEDPREADDER(0)
    ) dsp(
        .enable(1'b1),
        .clk(clk),
        .rst(rst),
        .A(A),
        .B(B),
        .C(Creg1),
        .ALUMODE(4'b0000),     // Z + W + X + Y + CIN
        .INMODE(5'b00000),     // M = A * B
        .OPMODE(9'b110000101), // XY = M, W = C, Z = 0
        .P(P)
    );

    // Output Part
    reg qa_of;
    reg qb_of;
    reg qc_of;
    reg qd_of;
    reg qa_sign;
    reg qb_sign;
    reg qc_sign;
    reg qd_sign;
    reg [5:0] qa_exp;
    reg [5:0] qb_exp;
    reg [5:0] qc_exp;
    reg [5:0] qd_exp;
    reg [5:0] qa_exp_reg;
    reg [5:0] qb_exp_reg;
    reg [5:0] qc_exp_reg;
    reg [5:0] qd_exp_reg;
    reg [7:0] qa_mant;
    reg [7:0] qb_mant;
    reg [7:0] qc_mant;
    reg [7:0] qd_mant;

    always @(posedge clk) begin
        if (e5m2mode) begin
            qa_exp_reg <= q_reg2[6:2] + a_reg2[6:2] + 6'd48;
            qb_exp_reg <= q_reg2[6:2] + b_reg2[6:2] + 6'd48;
            qc_exp_reg <= q_reg2[6:2] + c_reg2[6:2] + 6'd48;
            qd_exp_reg <= q_reg2[6:2] + d_reg2[6:2] + 6'd48;
        end else begin
            qa_exp_reg <= q_reg2[6:3] + a_reg2[6:3] + 6'd1;
            qb_exp_reg <= q_reg2[6:3] + b_reg2[6:3] + 6'd1;
            qc_exp_reg <= q_reg2[6:3] + c_reg2[6:3] + 6'd1;
            qd_exp_reg <= q_reg2[6:3] + d_reg2[6:3] + 6'd1;
        end
    end

    always @(*) begin
        qa_sign = q_reg3[7] ^ a_reg3[7];
        qb_sign = q_reg3[7] ^ b_reg3[7];
        qc_sign = q_reg3[7] ^ c_reg3[7];
        qd_sign = q_reg3[7] ^ d_reg3[7];
        if (e5m2mode) begin
            qa_mant = P[5:0];
            qb_mant = P[11:6];
            qc_mant = P[17:12];
            qd_mant = P[23:18];
        end else begin
            qa_mant = P[7:0];
            qb_mant = P[15:8];
            qc_mant = P[23:16];
            qd_mant = P[31:24];
        end
        qa_exp = qa_exp_reg + qa_mant[7];
        qb_exp = qb_exp_reg + qb_mant[7];
        qc_exp = qc_exp_reg + qc_mant[7];
        qd_exp = qd_exp_reg + qd_mant[7];
        qa_of = qa_exp[5] | (qa_exp[4:0] == 5'b11111);
        qb_of = qb_exp[5] | (qb_exp[4:0] == 5'b11111);
        qc_of = qc_exp[5] | (qc_exp[4:0] == 5'b11111);
        qd_of = qd_exp[5] | (qd_exp[4:0] == 5'b11111);

        qa_mant = qa_mant[7] ? {qa_mant[6:0], 1'b0} : {qa_mant[5:0], 2'b00};
        qb_mant = qb_mant[7] ? {qb_mant[6:0], 1'b0} : {qb_mant[5:0], 2'b00};
        qc_mant = qc_mant[7] ? {qc_mant[6:0], 1'b0} : {qc_mant[5:0], 2'b00};
        qd_mant = qd_mant[7] ? {qd_mant[6:0], 1'b0} : {qd_mant[5:0], 2'b00};
    end

    assign qa = qa_of ? {qa_sign, 5'b11111, 6'b0} : {qa_sign, qa_exp[4:0], qa_mant[7:2]};
    assign qb = qb_of ? {qb_sign, 5'b11111, 6'b0} : {qb_sign, qb_exp[4:0], qb_mant[7:2]};
    assign qc = qc_of ? {qc_sign, 5'b11111, 6'b0} : {qc_sign, qc_exp[4:0], qc_mant[7:2]};
    assign qd = qd_of ? {qd_sign, 5'b11111, 6'b0} : {qd_sign, qd_exp[4:0], qd_mant[7:2]};
endmodule


module FP8VectorMul2 (
    input clk,
    input rst,
    input e5m2mode,
    input in_valid,
    input [7:0] q,
    input [7:0] k,
    input [7:0] a,
    input [7:0] b,
    input [7:0] c,

    output [11:0] qa,
    output [11:0] qb,
    output [11:0] qc,
    output [11:0] ka,
    output [11:0] kb,
    output [11:0] kc,
    output reg out_valid
);
    // Input Part
    reg [7:0] a_reg1; reg [7:0] a_reg2; reg [7:0] a_reg3;
    reg [7:0] b_reg1; reg [7:0] b_reg2; reg [7:0] b_reg3;
    reg [7:0] c_reg1; reg [7:0] c_reg2; reg [7:0] c_reg3;

    wire [17:0] MantB;
    wire [29:0] MantA;
    wire [47:0] MantP;
    assign MantB = e5m2mode 
                ? {4'b0000, a[6:2]!=0, a[1:0], 2'b00, b[6:2]!=0, b[1:0], 2'b00, c[6:2]!=0, c[1:0]}
                : {a[6:2]!=0, a[2:0], 3'b000, b[6:2]!=0, b[2:0], 3'b000, c[6:2]!=0, c[2:0]};
    assign MantA = e5m2mode 
                ? { 3'b0, q[6:2]!=0, q[1:0], 21'b0, k[6:2]!=0, k[1:0]}
                : { 3'b0, q[2:0], 21'b0, k[2:0]};

    wire [47:0] ExpoAB;
    wire [47:0] ExpoC;
    wire [47:0] ExpoP;
    assign ExpoAB = e5m2mode
                ? {1'b0, a[7], 1'b0, b[7], 1'b0, c[7], 1'b0, a[7], 1'b0, b[7], 1'b0, c[7], 1'b0, a[6:2], 1'b0, b[6:2], 1'b0, c[6:2], 1'b0, a[6:2], 1'b0, b[6:2], 1'b0, c[6:2]}
                : {1'b0, a[7], 1'b0, b[7], 1'b0, c[7], 1'b0, a[7], 1'b0, b[7], 1'b0, c[7], 2'b0, a[6:3], 2'b0, b[6:3], 2'b0, c[6:3], 2'b0, a[6:3], 2'b0, b[6:3], 2'b0, c[6:3]};

    assign ExpoC = e5m2mode
                ? {1'b0, q[7], 1'b0, q[7], 1'b0, q[7], 1'b0, k[7], 1'b0, k[7], 1'b0, k[7], 1'b0, q[6:2], 1'b0, q[6:2], 1'b0, q[6:2], 1'b0, k[6:2], 1'b0, k[6:2], 1'b0, k[6:2]}
                : {1'b0, q[7], 1'b0, q[7], 1'b0, q[7], 1'b0, k[7], 1'b0, k[7], 1'b0, k[7], 2'b0, q[6:3], 2'b0, q[6:3], 2'b0, q[6:3], 2'b0, k[6:3], 2'b0, k[6:3], 2'b0, k[6:3]};

    reg out_valid_reg1;
    reg out_valid_reg2;

    always @(posedge clk) begin
        out_valid_reg1 <= in_valid;
        out_valid_reg2 <= out_valid_reg1;
        out_valid <= out_valid_reg2;
        a_reg1 <= a;
        a_reg2 <= a_reg1;
        a_reg3 <= a_reg2;
        b_reg1 <= b;
        b_reg2 <= b_reg1;
        b_reg3 <= b_reg2;
        c_reg1 <= c;
        c_reg2 <= c_reg1;
        c_reg3 <= c_reg2;
    end

    DSP #(
        .INPUTREG(1),
        .OUTPUTREG(1),
        .DSPPIPEREG(1),
        .CONTROLREG(0),
        .NEEDPREADDER(0)
    ) MantDSP (
        .enable(1'b1),
        .clk(clk),
        .rst(rst),
        .A(MantA),
        .B(MantB),
        .C(48'b0),
        .ALUMODE(4'b0000),     // Z + W + X + Y + CIN
        .INMODE(5'b10001),     // M = A * B
        .OPMODE(9'b000000101), // XY = M, W = C, Z = 0
        .P(MantP)
    );
    DSP #(
        .INPUTREG(1),
        .OUTPUTREG(1),
        .DSPPIPEREG(1),
        .CONTROLREG(0),
        .NEEDPREADDER(0)
    ) ExpoDSP (
        .enable(1'b1),
        .clk(clk),
        .rst(rst),
        .A(ExpoAB[47:18]),
        .B(ExpoAB[17:0]),
        .C(ExpoC),
        .ALUMODE(4'b0000),     // Z + W + X + Y + CIN
        .INMODE(5'b00000),     // M = A * B
        .OPMODE(9'b110000011), // X = A:B, Y = 0, W = C, Z = 0
        .P(ExpoP)
    );

    // Output Part
    reg qa_of;
    reg qb_of;
    reg qc_of;
    reg ka_of;
    reg kb_of;
    reg kc_of;
    reg qa_sign;
    reg qb_sign;
    reg qc_sign;
    reg ka_sign;
    reg kb_sign;
    reg kc_sign;
    reg [5:0] qa_exp_reg;
    reg [5:0] qb_exp_reg;
    reg [5:0] qc_exp_reg;
    reg [5:0] ka_exp_reg;
    reg [5:0] kb_exp_reg;
    reg [5:0] kc_exp_reg;
    reg [5:0] qa_exp;
    reg [5:0] qb_exp;
    reg [5:0] qc_exp;
    reg [5:0] ka_exp;
    reg [5:0] kb_exp;
    reg [5:0] kc_exp;
    reg [7:0] qa_mant;
    reg [7:0] qb_mant;
    reg [7:0] qc_mant;
    reg [7:0] ka_mant;
    reg [7:0] kb_mant;
    reg [7:0] kc_mant;

    always @(posedge clk) begin
        qa_sign <= ExpoP[46];
        qb_sign <= ExpoP[44];
        qc_sign <= ExpoP[42];
        ka_sign <= ExpoP[40];
        kb_sign <= ExpoP[38];
        kc_sign <= ExpoP[36];
        if (e5m2mode) begin
            qa_exp_reg <= ExpoP[35:30] + 6'd48;
            qb_exp_reg <= ExpoP[29:24] + 6'd48;
            qc_exp_reg <= ExpoP[23:18] + 6'd48;
            ka_exp_reg <= ExpoP[17:12] + 6'd48;
            kb_exp_reg <= ExpoP[11:6 ] + 6'd48;
            kc_exp_reg <= ExpoP[5 :0 ] + 6'd48;
        end else begin
            qa_exp_reg <= ExpoP[35:30] + 6'd1;
            qb_exp_reg <= ExpoP[29:24] + 6'd1;
            qc_exp_reg <= ExpoP[23:18] + 6'd1;
            ka_exp_reg <= ExpoP[17:12] + 6'd1;
            kb_exp_reg <= ExpoP[11:6 ] + 6'd1;
            kc_exp_reg <= ExpoP[5 :0 ] + 6'd1;
        end
    end

    always @(*) begin
        if (e5m2mode) begin
            qa_mant = {MantP[39:34], 2'b0};
            qb_mant = {MantP[33:28], 2'b0};
            qc_mant = {MantP[27:22], 2'b0};
            ka_mant = {MantP[17:12], 2'b0};
            kb_mant = {MantP[11:6 ], 2'b0};
            kc_mant = {MantP[5 :0 ], 2'b0};
        end else begin
            qa_mant = {1'b0, MantP[44:38]};
            qb_mant = {1'b0, MantP[37:31]};
            qc_mant = {1'b0, MantP[30:24]};
            ka_mant = {1'b0, MantP[20:14]};
            kb_mant = {1'b0, MantP[13:7 ]};
            kc_mant = {1'b0, MantP[6 :0 ]};
            
            if (a_reg3[6:3]>0) begin
                qa_mant[7:3] = qa_mant[7:3] + a_reg3[2:0];
                ka_mant[7:3] = ka_mant[7:3] + a_reg3[2:0];
            end
            if (b_reg3[6:3]>0) begin
                qb_mant[7:3] = qb_mant[7:3] + b_reg3[2:0];
                kb_mant[7:3] = kb_mant[7:3] + b_reg3[2:0];
            end
            if (c_reg3[6:3]>0) begin
                qc_mant[7:3] = qc_mant[7:3] + c_reg3[2:0];
                kc_mant[7:3] = kc_mant[7:3] + c_reg3[2:0];
            end
        end
        qa_exp = qa_exp_reg + qa_mant[7];
        qb_exp = qb_exp_reg + qb_mant[7];
        qc_exp = qc_exp_reg + qc_mant[7];
        ka_exp = ka_exp_reg + ka_mant[7];
        kb_exp = kb_exp_reg + kb_mant[7];
        kc_exp = kc_exp_reg + kc_mant[7];
        qa_of = qa_exp[5] | (qa_exp[4:0] == 5'b11111);
        qb_of = qb_exp[5] | (qb_exp[4:0] == 5'b11111);
        qc_of = qc_exp[5] | (qc_exp[4:0] == 5'b11111);
        ka_of = ka_exp[5] | (ka_exp[4:0] == 5'b11111);
        kb_of = kb_exp[5] | (kb_exp[4:0] == 5'b11111);
        kc_of = kc_exp[5] | (kc_exp[4:0] == 5'b11111);

        qa_mant = qa_mant[7] ? {qa_mant[6:0], 1'b0} : {qa_mant[5:0], 2'b00};
        qb_mant = qb_mant[7] ? {qb_mant[6:0], 1'b0} : {qb_mant[5:0], 2'b00};
        qc_mant = qc_mant[7] ? {qc_mant[6:0], 1'b0} : {qc_mant[5:0], 2'b00};
        ka_mant = ka_mant[7] ? {ka_mant[6:0], 1'b0} : {ka_mant[5:0], 2'b00};
        kb_mant = kb_mant[7] ? {kb_mant[6:0], 1'b0} : {kb_mant[5:0], 2'b00};
        kc_mant = kc_mant[7] ? {kc_mant[6:0], 1'b0} : {kc_mant[5:0], 2'b00};
    end

    assign qa = qa_of ? {qa_sign, 5'b11111, 6'b0} : {qa_sign, qa_exp[4:0], qa_mant[7:2]};
    assign qb = qb_of ? {qb_sign, 5'b11111, 6'b0} : {qb_sign, qb_exp[4:0], qb_mant[7:2]};
    assign qc = qc_of ? {qc_sign, 5'b11111, 6'b0} : {qc_sign, qc_exp[4:0], qc_mant[7:2]};
    assign ka = ka_of ? {ka_sign, 5'b11111, 6'b0} : {ka_sign, ka_exp[4:0], ka_mant[7:2]};
    assign kb = kb_of ? {kb_sign, 5'b11111, 6'b0} : {kb_sign, kb_exp[4:0], kb_mant[7:2]};
    assign kc = kc_of ? {kc_sign, 5'b11111, 6'b0} : {kc_sign, kc_exp[4:0], kc_mant[7:2]};
endmodule