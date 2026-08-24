module float_display #(
    parameter prefix = "Number",
    parameter EXP_BITS = 8,    // Number of exponent bits
    parameter MANT_BITS = 23   // Number of mantissa bits
)(
    input [EXP_BITS+MANT_BITS:0] float_num,  // Input floating point number (sign + exp + mant)
    output reg [63:0] decoded                 // Output decoded number
);
    localparam BIAS = (1 << (EXP_BITS-1)) - 1;

    reg sign;
    reg [EXP_BITS-1:0] exponent;
    reg [MANT_BITS-1:0] mantissa;
    real decoded_num;
    real mantissa_val;
    integer true_exp;
    // Verilog-2001: Vivado rejects a loop-local `integer` and `i++`.
    integer i;

    always @(float_num) begin
        sign = float_num[EXP_BITS+MANT_BITS];
        exponent = float_num[EXP_BITS+MANT_BITS-1:MANT_BITS];
        mantissa = float_num[MANT_BITS-1:0];
        // Convert mantissa to real value
        mantissa_val = 0.0;
        for (i = 0; i < MANT_BITS; i = i + 1) begin
            if (mantissa[i]) begin
                mantissa_val = mantissa_val + (2.0 ** (-1 * (MANT_BITS - i)));
            end
        end

        if (exponent == 0) begin
            // Denormalized number or zero
            if (mantissa == 0) begin
                if(prefix!="") begin
                    $display("%s: 0.0 (%b)", prefix, float_num);
                end
            end else begin
                true_exp = 1 - BIAS;
                decoded_num = mantissa_val * (2.0 ** true_exp);
                if (sign) begin
                    decoded_num = -decoded_num;
                end
                if(prefix!="") begin
                    $display("%s (denorm): %g (%b)", prefix, decoded_num, float_num);
                end
            end
        end
        else if (exponent == {EXP_BITS{1'b1}}) begin
            // Infinity or NaN
            if (mantissa == 0) begin
                decoded_num = $bitstoreal({sign, 11'b11111111111, 52'b0});
                if(prefix!="") begin
                    $display("%s: %sInfinity (%b)", prefix, sign ? "-" : "+", float_num);
                end
            end else begin
                decoded_num = $bitstoreal(-64'b1);
                if(prefix!="") begin
                    $display("%s: NaN (%b)", prefix, float_num);
                end
            end
        end
        else begin
            // Normal number
            true_exp = exponent - BIAS;
            decoded_num = (1.0 + mantissa_val) * (2.0 ** true_exp);
            if (sign) begin
                decoded_num = -decoded_num;
            end
            if(prefix!="") begin
                $display("%s: %g (%b)", prefix, decoded_num, float_num);
            end
        end
        decoded = $realtobits(decoded_num);
    end

endmodule