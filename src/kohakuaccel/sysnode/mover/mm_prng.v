// Philox-4x32-10: a counter-based PRNG, one round per cycle.
//
// docs/memory-mover/prng.md s3. Counter-based and stateless, so the value is a
// pure function of (key, counter) -- which is what makes noise independent of
// how a region was tiled, restartable after a fault, and parallelisable by
// handing each lane a different counter. A stateful LFSR has none of those and
// would make a scheduling change look like a numerical bug.
//
// ITERATIVE, NOT UNROLLED. Ten rounds unrolled is twenty 32x32 multiplies in
// one path; at four cycles per round it is two, reused, for 40 cycles per 128
// bits. A 128x128x4 FP16 latent is 4096 256-bit words, so a full draw is 8192
// outputs -- 328k cycles, 1.1 ms at 300 MHz, against a UNet step measured in
// tens of ms. A few percent, and the first lever if it ever matters is a
// second generator, not a shorter round.
//
// Philox is chosen over Threefry because the constraint here is FABRIC, not
// multipliers: the vector core extrapolates to ~37% of an SLR's LUTs against
// 12.5% of its DSPs (compute/vector-core.md s13). Threefry is the ARX design
// that trades multipliers for rounds, which is why XLA prefers it on TPU.

`default_nettype none

module mm_prng #(
    parameter integer ROUNDS = 10
)(
    input  wire         clk,
    input  wire         rst,

    input  wire         start,
    input  wire [63:0]  key_in,
    input  wire [127:0] ctr_in,

    output reg          busy,
    output reg          out_valid,
    output wire [127:0] out
);
    localparam [31:0] M0 = 32'hD251_1F53;
    localparam [31:0] M1 = 32'hCD9E_8D57;
    localparam [31:0] W0 = 32'h9E37_79B9;
    localparam [31:0] W1 = 32'hBB67_AE85;

    reg [31:0] c0, c1, c2, c3;
    reg [31:0] k0, k1;
    reg [4:0]  round;

    // TWO CYCLES PER ROUND, and the seam is after the multiply. In one cycle
    // synthesis absorbs the counter registers into the DSPs' own P registers,
    // which leaves the XOR sitting directly between two multipliers with no
    // register anywhere -- measured at 261 MHz against a 300 MHz target. The
    // seam costs 20 cycles per 128 bits, and rate is not what is scarce: a
    // latent-sized draw is still well under a millisecond.
    // FOUR cycles per round, because a 32x32 product is a DSP48E2 cascade and
    // its carry out to fabric costs 3.34 ns -- 299.5 MHz, a miss however
    // narrow, and the same path limits mm_mover. M is constant, so the mulhilo
    // splits into two 16x32 partials, M*c = M_lo*c + (M_hi*c << 16), with a
    // register between the partials and their sum.
    reg [1:0]  phase;
    reg [31:0] m_a0, m_a2;
    reg [31:0] r_hi0, r_lo0, r_hi1, r_lo1;
    reg [47:0] pl0, ph0, pl1, ph1;

    // The key is bumped AFTER each round rather than before, which is the same
    // schedule -- round 0 uses the loaded key, round n uses key + n*W -- but
    // keeps the adder and its mux OFF the DSP-to-DSP path. In series with the
    // XOR they cost 0.5 ns and the target by 39 MHz.

    wire [31:0] n0 = r_hi1 ^ c1 ^ k0;
    wire [31:0] n1 = r_lo1;
    wire [31:0] n2 = r_hi0 ^ c3 ^ k1;
    wire [31:0] n3 = r_lo0;

    assign out = {c3, c2, c1, c0};

    always @(posedge clk) begin
        // The counter, key and round state are all loaded from ctr_in/key_in by
        // `start` before a round reads them, and `out` waits for out_valid.
        if (rst) begin
            busy <= 1'b0; out_valid <= 1'b0; round <= 5'd0; phase <= 2'd0;
        end else begin
            out_valid <= 1'b0;
            if (start && !busy) begin
                c0 <= ctr_in[31:0];   c1 <= ctr_in[63:32];
                c2 <= ctr_in[95:64];  c3 <= ctr_in[127:96];
                k0 <= key_in[31:0];   k1 <= key_in[63:32];
                round <= 5'd0;
                phase <= 2'd0;
                busy  <= 1'b1;
            end else if (busy) begin
                case (phase)
                    2'd0: begin
                        m_a0  <= c0;
                        m_a2  <= c2;
                        phase <= 2'd1;
                    end
                    2'd1: begin
                        pl0 <= M0[15:0]  * m_a0;
                        ph0 <= M0[31:16] * m_a0;
                        pl1 <= M1[15:0]  * m_a2;
                        ph1 <= M1[31:16] * m_a2;
                        phase <= 2'd2;
                    end
                    2'd2: begin
                        {r_hi0, r_lo0} <= {16'd0, pl0} + {ph0, 16'd0};
                        {r_hi1, r_lo1} <= {16'd0, pl1} + {ph1, 16'd0};
                        phase <= 2'd3;
                    end
                    default: begin
                        c0 <= n0; c1 <= n1; c2 <= n2; c3 <= n3;
                        k0 <= k0 + W0; k1 <= k1 + W1;
                        phase <= 2'd0;
                        if (round + 5'd1 == ROUNDS[4:0]) begin
                            busy      <= 1'b0;
                            out_valid <= 1'b1;
                        end
                        round <= round + 5'd1;
                    end
                endcase
            end
        end
    end

endmodule

`default_nettype wire
