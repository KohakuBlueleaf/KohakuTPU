// khs_ffold -- combine an accumulator's NPART partials into one value per slot.
//
// SERIAL THROUGH THE LANE, AND DELIBERATELY SO. Each step depends on the last,
// so the steps are ALAT apart: NPART*ALAT cycles, 96 at NPART 16. That runs
// ONCE per reduction against a kernel of thousands of cycles, and the
// alternative -- log2(NPART) float adders of their own -- is hardware that
// stands idle the rest of the time and rounds differently from the accumulate
// path it is supposed to finish.
//
// EVERY SLOT FOLDS AT ONCE. Each slot owns a lane, so the whole accumulator
// folds in the time one slot takes.
//
// The order is the ISA's: partial 0 first, then 1, and so on. Float addition
// does not associate, so this loop direction is contract and the golden model
// walks it the same way.

`default_nettype none

module khs_ffold #(
    parameter integer NPART = 16,
    parameter integer ALAT  = 6
)(
    input  wire                     clk,
    input  wire                     resetn,

    input  wire                     start,
    output wire                     busy,
    output wire                     done,        // one cycle, result valid

    // to the accumulator: which partial to present
    output wire [$clog2(NPART)-1:0] part_idx,

    // to the lanes: issue one step, with the running total as the addend
    output wire                     iss_valid,
    output wire                     iss_raw
);
    localparam integer PW = (NPART > 1) ? $clog2(NPART) : 1;
    localparam integer TW = $clog2(ALAT + 1);

    reg           run;
    reg [PW-1:0]  k;
    reg [TW-1:0]  wait_n;
    reg           fire;
    reg           fin;

    assign busy      = run;
    assign done      = fin;
    assign part_idx  = k;
    assign iss_valid = fire;
    assign iss_raw   = fire;

    always @(posedge clk) begin
        if (!resetn) begin
            run <= 1'b0; k <= {PW{1'b0}}; wait_n <= {TW{1'b0}};
            fire <= 1'b0; fin <= 1'b0;
        end else begin
            fire <= 1'b0;
            fin  <= 1'b0;

            if (!run) begin
                if (start) begin
                    run    <= 1'b1;
                    k      <= {PW{1'b0}};
                    wait_n <= {TW{1'b0}};
                    fire   <= 1'b1;          // step 0 issues immediately
                end
            end else if (wait_n != ALAT[TW-1:0]) begin
                wait_n <= wait_n + 1'b1;
            end else begin
                // The previous step's result is at the lane's output now, and
                // the accumulator captured it as the running total.
                wait_n <= {TW{1'b0}};
                if (k == (NPART-1)) begin
                    run <= 1'b0;
                    fin <= 1'b1;
                end else begin
                    k    <= k + 1'b1;
                    fire <= 1'b1;
                end
            end
        end
    end

endmodule

`default_nettype wire
