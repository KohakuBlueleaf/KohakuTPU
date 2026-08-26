// RV64M: the multiply family and the divide family, multi-cycle.
//
// MULTIPLY IS FOUR 32x32 STEPS, NOT ONE 64x64. A flat 64x64 wants ~16 DSP48s
// and the node's ceiling is 48 with 35 already spent -- one 32x32 reused over
// four cycles is ~4, and a control processor does not multiply on a hot path.
//
// DIVIDE IS RESTORING, ONE BIT PER CYCLE: 64 iterations over one 65-bit
// subtract, on magnitudes, with the signs reapplied at the end.
//
// A SEPARATE FINISH STATE. Both loops leave their answer in registers that are
// only settled on the cycle after the last iteration, so the result is composed
// there and `y` has exactly one writer.

`default_nettype none

module rv64_muldiv (
    input  wire        clk,
    input  wire        resetn,

    input  wire        start,
    input  wire [2:0]  f3,
    input  wire        word,
    input  wire [63:0] a,
    input  wire [63:0] b,

    output wire        busy,
    output reg         done,
    output reg  [63:0] y
);
    localparam [2:0] F_MUL = 3'd0, F_MULH = 3'd1, F_MULHSU = 3'd2;
    localparam [2:0] F_MULHU = 3'd3, F_DIV = 3'd4, F_DIVU = 3'd5;
    localparam [2:0] F_REM = 3'd6, F_REMU = 3'd7;

    localparam [1:0] S_IDLE = 2'd0, S_MUL = 2'd1, S_DIV = 2'd2, S_FIN = 2'd3;
    reg [1:0] state;
    assign busy = (state != S_IDLE);

    // DIVW SIGN-extends its operands and DIVUW ZERO-extends them; reversing
    // that is the classic RV64M bug and it shows only on negative inputs.
    wire a_sgn = (f3 == F_MUL) || (f3 == F_MULH) || (f3 == F_MULHSU)
              || (f3 == F_DIV) || (f3 == F_REM);
    wire b_sgn = (f3 == F_MUL) || (f3 == F_MULH)
              || (f3 == F_DIV) || (f3 == F_REM);

    wire [63:0] a_w = a_sgn ? {{32{a[31]}}, a[31:0]} : {32'd0, a[31:0]};
    wire [63:0] b_w = b_sgn ? {{32{b[31]}}, b[31:0]} : {32'd0, b[31:0]};
    wire [63:0] ax  = word ? a_w : a;
    wire [63:0] bx  = word ? b_w : b;

    reg  [2:0]  rf3;
    reg         rword;
    reg  [6:0]  cnt;
    reg  [63:0] ra, rb, sa, sb;      // sa/sb keep the ORIGINAL operands
    reg         a_neg, b_neg;

    // ---- multiply -----------------------------------------------------------
    reg [127:0] acc;
    reg [31:0]  ma, mb;
    always @(*) begin
        case (cnt[1:0])
            2'd0:    begin ma = ra[31:0];  mb = rb[31:0];  end
            2'd1:    begin ma = ra[63:32]; mb = rb[31:0];  end
            2'd2:    begin ma = ra[31:0];  mb = rb[63:32]; end
            default: begin ma = ra[63:32]; mb = rb[63:32]; end
        endcase
    end
    // TWO REGISTERS ROUND THE MULTIPLIER, and they are the difference between
    // using a DSP48 and abusing one. MEASURED unregistered: the binding path was
    // cnt -> operand mux -> DSP -> accumulator add, 23 logic levels and
    // 11 CARRY8 in ONE cycle, holding the core to 220 MHz. Registered, the
    // multiply costs two more cycles and the path is three short ones.
    reg [31:0] ma_q, mb_q;
    reg [63:0] prod;
    // The product's own range and validity travel with it, two deep.
    reg [1:0]  sel_d1, acc_sel;
    reg        v_d1, acc_v;
    always @(posedge clk) begin
        ma_q <= ma;
        mb_q <= mb;
        prod <= ma_q * mb_q;
        if (!resetn) begin
            v_d1  <= 1'b0;
            acc_v <= 1'b0;
        end
        else begin
            v_d1    <= (state == S_MUL) && (cnt <= 7'd3);
            sel_d1  <= cnt[1:0];
            acc_v   <= v_d1;
            acc_sel <= sel_d1;
        end
    end

    // SIGN CORRECTION IS 64 BITS WIDE, NOT 128. Both subtrahends are shifted
    // left 64, so they cannot borrow into the low half -- the low half is `acc`
    // untouched and only the high half needs an adder.
    wire [63:0] corr_hi = acc[127:64]
                        - (a_neg ? sb : 64'd0)
                        - (b_neg ? sa : 64'd0);

    // ---- divide -------------------------------------------------------------
    reg  [63:0] quo;
    reg  [64:0] rem;
    wire [64:0] sh_rem = {rem[63:0], ra[63]};
    wire [64:0] diff   = sh_rem - {1'b0, rb};
    wire        fits   = !diff[64];

    wire div_zero = (rb == 64'd0);
    wire ovf = ((rf3 == F_DIV) || (rf3 == F_REM))
            && (sa == 64'h8000_0000_0000_0000) && (&sb);

    wire [63:0] q_signed = (a_neg ^ b_neg) ? (~quo + 64'd1) : quo;
    wire [63:0] r_signed = a_neg ? (~rem[63:0] + 64'd1) : rem[63:0];

    reg [63:0] fin;
    always @(*) begin
        if (rf3[2]) begin
            if (div_zero) begin
                fin = ((rf3 == F_DIV) || (rf3 == F_DIVU)) ? {64{1'b1}} : sa;
            end else if (ovf) begin
                fin = (rf3 == F_DIV) ? 64'h8000_0000_0000_0000 : 64'd0;
            end else begin
                case (rf3)
                    F_DIV:   fin = q_signed;
                    F_DIVU:  fin = quo;
                    F_REM:   fin = r_signed;
                    default: fin = rem[63:0];
                endcase
            end
        end
        else begin
            // MULW takes the LOW word; there is no MULHW.
            case (rf3)
                F_MUL:   fin = acc[63:0];
                default: fin = corr_hi;
            endcase
        end
    end

    always @(posedge clk) begin
        if (!resetn) begin
            state <= S_IDLE;
            done  <= 1'b0;
            cnt   <= 7'd0;
        end
        else begin
            done <= 1'b0;
            case (state)
                S_IDLE: if (start) begin
                    rf3   <= f3;
                    rword <= word;
                    cnt   <= 7'd0;
                    acc   <= 128'd0;
                    quo   <= 64'd0;
                    rem   <= 65'd0;
                    sa    <= ax;
                    sb    <= bx;
                    if (f3[2]) begin
                        a_neg <= ((f3 == F_DIV) || (f3 == F_REM)) && ax[63];
                        b_neg <= ((f3 == F_DIV) || (f3 == F_REM)) && bx[63];
                        ra <= (((f3 == F_DIV) || (f3 == F_REM)) && ax[63])
                              ? (~ax + 64'd1) : ax;
                        rb <= (((f3 == F_DIV) || (f3 == F_REM)) && bx[63])
                              ? (~bx + 64'd1) : bx;
                        state <= S_DIV;
                    end
                    else begin
                        // The multiply is unsigned; `a_neg`/`b_neg` drive the
                        // correction rather than the operands.
                        a_neg <= a_sgn && ax[63];
                        b_neg <= b_sgn && bx[63];
                        ra    <= ax;
                        rb    <= bx;
                        state <= S_MUL;
                    end
                end

                // SIX CYCLES: four operand pairs issued, and the product of the
                // last one lands two later. Each partial lands in its own range
                // so the adder is as wide as the range, not as wide as the
                // product -- step 0 is a plain assign, 1 and 2 are 96 bits,
                // 3 is 64.
                S_MUL: begin
                    if (acc_v) begin
                        case (acc_sel)
                            2'd0:    acc[63:0]   <= prod;
                            2'd1,
                            2'd2:    acc[127:32] <= acc[127:32] + {32'd0, prod};
                            default: acc[127:64] <= acc[127:64] + prod;
                        endcase
                    end
                    cnt <= cnt + 7'd1;
                    if (cnt == 7'd5) begin
                        state <= S_FIN;
                    end
                end

                S_DIV: begin
                    rem <= fits ? diff : sh_rem;
                    quo <= {quo[62:0], fits};
                    ra  <= {ra[62:0], 1'b0};
                    cnt <= cnt + 7'd1;
                    if (cnt == 7'd63) begin
                        state <= S_FIN;
                    end
                end

                default: begin
                    y     <= rword ? {{32{fin[31]}}, fin[31:0]} : fin;
                    done  <= 1'b1;
                    state <= S_IDLE;
                end
            endcase
        end
    end

endmodule

`default_nettype wire
