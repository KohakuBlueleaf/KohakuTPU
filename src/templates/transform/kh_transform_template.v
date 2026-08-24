// The memory-port transform slot, as a working identity transform.
// Contract: docs/spec/transform-slot.md. Production example: kohakutpu/
// transform/mx_quant.v (FP16 -> MXFP7). Replace the marked section only.
`default_nettype none

module kh_transform_template #(
    // Source entry length in beats. The output is ALWAYS four words -- rule 1
    // of the slot: the emitter and the L1 fill protocol assume it.
    parameter integer DATA_W   = 256,
    parameter integer IN_BEATS = 4
)(
    input  wire              clk,
    input  wire              rst,

    input  wire              start,      // one-cycle pulse; cfg valid with it
    input  wire              b_layout,   // your config bit(s); meaning is yours

    input  wire [DATA_W-1:0] beat,       // registered by the port already
    input  wire              beat_valid, // pushed at line rate, no handshake
    output wire              need_beat,  // reserved; drive truthfully or high

    output reg               done,       // one-cycle pulse: words are final
    output reg  [DATA_W-1:0] word0,
    output reg  [DATA_W-1:0] word1,
    output reg  [DATA_W-1:0] word2,
    output reg  [DATA_W-1:0] word3
);
    assign need_beat = 1'b1;

    localparam integer CW = (IN_BEATS <= 2) ? 1 : (IN_BEATS <= 4) ? 2 : (IN_BEATS <= 8) ? 3 : 4;
    reg [CW:0] cnt;
    reg        cfg_b;
    reg        run;

    // ---- your transform state goes here --------------------------------
    // Identity: the four most recent beats become the four words. A real
    // transform may hold the WHOLE entry before deciding (the quantiser's
    // block scale needs all beats) -- done may trail the last beat freely.
    (* EXTRACT_RESET = "no" *) reg [DATA_W-1:0] acc0, acc1, acc2, acc3;

    always @(posedge clk) begin
        done <= 1'b0;
        if (rst) begin
            run <= 1'b0;
            cnt <= 0;
        end else begin
            if (start) begin
                run   <= 1'b1;
                cnt   <= 0;
                cfg_b <= b_layout;
            end
            if (run && beat_valid) begin
                // ---- your per-beat datapath ----------------------------
                acc0 <= acc1;
                acc1 <= acc2;
                acc2 <= acc3;
                acc3 <= beat;
                // --------------------------------------------------------
                if (cnt == IN_BEATS - 1) begin
                    run <= 1'b0;
                end else begin
                    cnt <= cnt + 1'b1;
                end
            end
            // Emit one cycle after the last beat. Words must stay stable
            // from `done` until the next `start` -- the port latches them.
            if (run && beat_valid && cnt == IN_BEATS - 1) begin
                word0 <= acc1;
                word1 <= acc2;
                word2 <= acc3;
                word3 <= beat;
                done  <= 1'b1;
            end
        end
    end

endmodule

`default_nettype wire
