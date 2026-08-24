// What the float lane's LIVE OPCODE SET costs. Both PEs instantiate the same
// `khs_float_lane`; only the number of `vec_alu` opcodes reaching `op` differs.
//
//   NOPS = 1    a constant FMA          kht_fpu at HAS_FSFU = 0
//   NOPS = 5    FMA + the four seeds    kht_fpu at HAS_FSFU = 1
//   NOPS = 13   what khs_falu issues    the SIMD tier
//
// SIMT maps vfmul/vfadd/vfsub ONTO the FMA and has no vfmin/vfmax/vfcmp at all,
// so no parameter of either PE equalises the sets -- this prices the gap.
//
// `raw_e8` is tied off as kht_fpu ties it; the SIMD tier drives it for the
// accumulator fold, so the 13 row holds only at SIMD_FACC = 0.

`default_nettype none

module khg_opcost #(
    parameter integer NOPS = 1
)(
    input  wire        clk,
    input  wire        rst,
    input  wire        in_valid,
    input  wire [3:0]  sel,
    input  wire        wide,
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire [23:0] c,
    output wire        out_valid,
    output wire [23:0] out,
    output wire        out_pred
);
    // vec_alu's own opcode numbers, spelled here because this probe includes no
    // ISA header -- they are checked against vec_alu.v's localparams by name.
    localparam [4:0] O_ADD = 5'd3,  O_SUB = 5'd4,  O_MUL = 5'd5,  O_FMA = 5'd6;
    localparam [4:0] O_MAX = 5'd8,  O_MIN = 5'd9;
    localparam [4:0] O_CLT = 5'd11, O_CGT = 5'd12, O_CEQ = 5'd13;
    localparam [4:0] O_EXP = 5'd16, O_LOG = 5'd17, O_INV = 5'd18, O_RSQ = 5'd19;

    reg [4:0] op;
    always @(*) begin
        if (NOPS == 1) begin
            op = O_FMA;
        end
        else if (NOPS == 5) begin
            case (sel[2:0])
                3'd0:    op = O_FMA;
                3'd1:    op = O_EXP;
                3'd2:    op = O_LOG;
                3'd3:    op = O_INV;
                default: op = O_RSQ;
            endcase
        end else begin
            case (sel)
                4'd0:    op = O_ADD;
                4'd1:    op = O_SUB;
                4'd2:    op = O_MUL;
                4'd3:    op = O_FMA;
                4'd4:    op = O_MAX;
                4'd5:    op = O_MIN;
                4'd6:    op = O_CLT;
                4'd7:    op = O_CGT;
                4'd8:    op = O_CEQ;
                4'd9:    op = O_EXP;
                4'd10:   op = O_LOG;
                4'd11:   op = O_INV;
                default: op = O_RSQ;
            endcase
        end
    end

    khs_float_lane u_lane (
        .clk(clk), .rst(rst),
        .in_valid(in_valid), .op(op), .wide(wide),
        .a(a), .b(b), .c(c),
        .raw_e8(1'b0), .a_e8(24'd0),
        .out_valid(out_valid), .out(out), .out_pred(out_pred)
    );

endmodule

`default_nettype wire
