// Accumulator format -> E8M15, so a vector lane can take a peer transfer.
//
// A cluster's peer port carries one 4x4 sub-tile at the ACCUMULATOR's width:
// mx_acu_fp.v's peer_in/peer_out are 16*(ACC_MW+8) bits, S1 E7 M{ACC_MW} with
// BIAS=63. ACC_MW=14 is FP22 (the default), 16 is FP24. That is a MESH format,
// never a memory one -- a DRAIN still writes FP16 (isa/cluster.md s5).
//
// The conversion is range-lossless in one direction only, and that is the whole
// reason it is cheap:
//
//     E7 spans real exponents -63..64;  E8 spans -126..127
//     so e8 = e7 + 64 always lands inside, and there is no overflow path
//
// So no saturation logic, no infinity generation, no range check. What it costs
// is the mantissa: M14 -> M15 is a zero-extend and EXACT, M16 -> M15 is one
// round-to-nearest-even whose carry can only reach e8 = 191, still normal.
//
// Neither format has subnormals, so E == 0 is zero and needs no fixup.
//
// Combinational. Latency is the caller's to pipeline; at ACC_MW=14 there is no
// adder in the mantissa path at all.

`default_nettype none

module vec_cvt_acc #(
    parameter integer ACC_MW = 14
)(
    input  wire [ACC_MW+7:0] acc,
    output wire [23:0]       e8
);
    localparam integer W = ACC_MW + 8;

    wire              s  = acc[W-1];
    wire [6:0]        e7 = acc[ACC_MW+6 -: 7];
    wire [ACC_MW-1:0] m  = acc[ACC_MW-1:0];

    wire is_zero = (e7 == 7'd0);
    wire is_max  = (e7 == 7'd127);          // inf when m == 0, NaN otherwise

    //: bits the mantissa must lose. Zero or negative means it only gains them.
    localparam integer DROP = ACC_MW - 15;

    wire [15:0] m_round;                    // {carry, 15 bits}
    wire [14:0] m_raw;                      // the same 15 bits, UNROUNDED

    generate
        if (DROP > 0) begin : g_raw
            assign m_raw = m[ACC_MW-1 -: 15];
        end else begin : g_raw_ext
            assign m_raw = {m, {(15 - ACC_MW){1'b0}}};
        end
    endgenerate

    generate
        if (DROP > 0) begin : g_round
            wire [14:0] keep = m[ACC_MW-1 -: 15];
            wire        g    = m[DROP-1];
            // one dropped bit is exactly a tie, so there is no sticky to form
            wire sticky = (DROP >= 2) ? |m[(DROP >= 2 ? DROP - 2 : 0):0] : 1'b0;
            assign m_round = {1'b0, keep} + (g & (sticky | keep[0]));
        end else if (DROP == 0) begin : g_same
            assign m_round = {1'b0, m};
        end else begin : g_extend
            assign m_round = {1'b0, m, {(-DROP){1'b0}}};
        end
    endgenerate

    wire [8:0] e_sum = {2'b00, e7} + 9'd64 + {8'd0, m_round[15]};

    // A NaN keeps its payload UNROUNDED: rounding one to zero would turn it
    // into an infinity, so the OR keeps it a NaN when the top bits are all low.
    wire [14:0] nan_pay = m_raw | {14'd0, (|m) & ~(|m_raw)};

    assign e8 = is_zero ? {s, 8'd0, 15'd0}
              : is_max  ? {s, 8'hFF, nan_pay}
                        : {s, e_sum[7:0], m_round[14:0]};

endmodule

`default_nettype wire
