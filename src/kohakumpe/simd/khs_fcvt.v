// One `vfcvt` element: binary32 <-> int32, and nothing else.
//
// FP32 IS THE ONLY COMPUTE TYPE, so there is no hub format and no `f2f`: the
// E8M15 pivot the four `vec_cvt` modules provided is gone from KohakuMPE along
// with the second float format that needed it. Both directions here are plain
// integer arithmetic against the same 24-bit significand rv_fpu unpacks.
//
// BIT-EXACT AGAINST THE MODEL. `khs_fp32.f2i` and `.i2f` are the reference and
// the bench grades on the bits, so every corner below is theirs: truncation
// toward zero, a NaN giving zero, saturation at int32's bounds with -2^31
// landing exactly, and round-to-nearest-even coming back.
//
// ONE CYCLE DEEP, AND THAT IS A TIMING FIX. Combinational, this was the binding
// path of the whole PE: vector file -> here -> the staging register, MEASURED
// at 20 logic levels and 8 CARRY8 for -0.772 ns, holding a 324.5 MHz build to
// 243.6. The cut puts the 32-bit negate and the leading-one in stage 1 and the
// barrel shifts and the round adder in stage 2, so no two long carry chains
// share a stage.

`default_nettype none

module khs_fcvt (
    input  wire        clk,
    input  wire [1:0]  op,     // KHF_FCVT_FCVT_{F2I,I2F}[1:0]
    input  wire [31:0] a,
    output wire [31:0] y      // one cycle after `op`/`a`
);
    localparam [1:0] OP_F2I = 2'd0;

    // ---- stage 1: the wide negate, the leading-one, the exponent decode -----
    wire        fs = a[31];
    wire [7:0]  fe = a[30:23];
    wire [22:0] fm = a[22:0];

    wire f_max = (fe == 8'hFF);
    // THE EXPONENT DECIDES SATURATION ON ITS OWN: e > 157 is |x| >= 2^31 for
    // every significand and e <= 157 never reaches it, so no wide compare.
    // value = sig * 2^(e-150), so placing sig at bit 7 of a 31-bit field and
    // shifting RIGHT by (rs-25) is the same number with one shifter and no
    // direction mux. rs is at least 25 wherever the result is representable.
    wire [7:0] rs = 8'd182 - fe;

    wire        is = a[31];
    wire [31:0] imag = is ? (~a + 32'd1) : a;
    wire [4:0]  ik;
    wire        inz;
    khs_lead1 #(.W(32)) u_l1 (.x(imag), .pos(ik), .nz(inz));

    reg  [1:0]  q_op;
    reg  [23:0] q_fsig;
    reg  [5:0]  q_rsx;
    reg         q_fs, q_fz, q_fnan, q_fovf;
    reg  [31:0] q_imag;
    reg  [4:0]  q_ik;
    reg         q_inz, q_is;
    always @(posedge clk) begin
        q_op   <= op;
        q_fsig <= {1'b1, fm};
        q_rsx  <= (rs > 8'd63) ? 6'd38 : (rs[5:0] - 6'd25);
        q_fs   <= fs;
        q_fz   <= (fe == 8'd0);
        q_fnan <= f_max && (fm != 23'd0);
        q_fovf <= f_max || (fe > 8'd157);
        q_imag <= imag;
        q_ik   <= ik;
        q_inz  <= inz;
        q_is   <= is;
    end

    // ---- stage 2: the shifts, the round, the pack --------------------------
    wire [30:0] fmag = {q_fsig, 7'b0} >> q_rsx;
    wire [31:0] fsat = q_fs ? 32'h8000_0000 : 32'h7FFF_FFFF;

    wire [31:0] i_from_f = (q_fz || q_fnan) ? 32'd0
                         : q_fovf           ? fsat
                         : q_fs             ? (~{1'b0, fmag} + 32'd1)
                                            : {1'b0, fmag};

    wire [31:0] inrm = q_imag << (5'd31 - q_ik);
    wire [23:0] ikeep = inrm[31:8];
    wire        iup = inrm[7] & ((|inrm[6:0]) | ikeep[0]);
    wire [24:0] isr = {1'b0, ikeep} + {24'b0, iup};
    wire        icy = isr[24];
    wire [22:0] ifr = icy ? isr[23:1] : isr[22:0];
    wire [7:0]  iex = {3'b0, q_ik} + (icy ? 8'd128 : 8'd127);

    wire [31:0] f_from_i = q_inz ? {q_is, iex, ifr} : 32'd0;

    assign y = (q_op == OP_F2I) ? i_from_f : f_from_i;
endmodule

`default_nettype wire
