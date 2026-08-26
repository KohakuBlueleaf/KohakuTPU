// kht_lds -- the banked shared memory: LANES banks, word-interleaved, with the
// bank-conflict resolver and its sequencer inside.
//
// THE WHOLE POINT IS THAT CONFLICT-FREE IS ONE PASS. A serial walk costs LANES
// accesses whatever the addresses are; this costs the MAXIMUM NUMBER OF LANES
// LANDING ON ONE BANK, which for the accesses a shader actually makes -- lane i
// touching element i, or a row of a matrix -- is one.
//
//   addr[LNW-1:0]  is the bank        so consecutive words are different banks
//   addr >> LNW    is the row within it
//
// That interleave is the reason stride-1 is conflict-free and stride-LANES is
// the worst case, and it is the same trade every GPU makes.
//
// THE RESOLVER LIVES HERE, not in the core, so the gate is one parameter and so
// the LANES x LANES comparison it costs is measured as part of this block
// rather than smeared into the LSU. It is the dominant cost at wide LANES and
// the ladder is what says by how much.
//
// FORWARD PROGRESS, which the sequencer rests on: every pass serves the lowest
// outstanding lane, because that lane is by construction the lowest lane on its
// own bank. So a sequence terminates in at most LANES passes and cannot stall.
// The bench asserts that rather than trusting this comment.

`default_nettype none

module kht_lds #(
    parameter integer LANES    = 8,
    parameter integer WORDS    = 2048,
    parameter         MEM_PRIM = "block",
    // BANKS IS A WIDTH AND THE DRAIN ALREADY EXISTS. The sequencer below walks
    // passes until `todo` empties, so fewer banks is simply more conflicts and
    // more passes -- no new mechanism, and the ISA never learns the count.
    // 0 = one bank per lane, which is every build before this parameter.
    parameter integer BANKS    = 0,
    parameter integer LNW      = (LANES > 1) ? $clog2(LANES) : 1,
    parameter integer AW       = $clog2(WORDS)
)(
    input  wire                clk,
    input  wire                resetn,

    // ---- the window side: linear word addresses, exactly as rv_spad ----
    input  wire                a_en,
    input  wire [3:0]          a_we,
    input  wire [AW-1:0]       a_addr,
    input  wire [31:0]         a_wdata,

    // ---- the core side: one address per lane, resolved here ----
    input  wire                req,
    input  wire                we,
    input  wire [LANES-1:0]    lmask,
    input  wire [LANES*AW-1:0] laddr,
    input  wire [LANES*32-1:0] lwdata,
    output reg  [LANES*32-1:0] lrdata,
    output wire                busy,
    output wire                done,
    // The witness: passes taken, and lane-accesses served. Conflict-free is one
    // pass per access whatever LANES is; all-one-bank is LANES.
    output reg  [31:0]         pass_ctr
);
    // -1 IS FULL RATE, one bank per lane. 0 is not built, and the caller does
    // not instantiate this at all there, so 0 folding to 1 is only a guard
    // against a width of zero reaching $clog2.
    localparam integer NB  = (BANKS < 0) ? LANES : ((BANKS == 0) ? 1 : BANKS);
    // NBS IS THE BANK BITS ACTUALLY USED AND NBW IS ONLY THE STORAGE WIDTH. At
    // ONE bank there is no bank field: taking a bit anyway made `lbank` reach
    // selv[1] on a one-entry array, the resolver served nobody, and the sequencer
    // spun -- the bench hung rather than failing a check.
    localparam integer NBS = (NB > 1) ? $clog2(NB) : 0;
    localparam integer NBW = (NBS > 0) ? NBS : 1;
    localparam integer BW  = AW - NBS;         // row address within a bank

    // ---- the outstanding set -------------------------------------------
    reg  [LANES-1:0] todo;
    reg              run, ph;                 // ph: 0 drives the arrays, 1 takes
    // The last pass's words land at the edge that ENDS phase 1, so `done` is a
    // cycle later than "no lanes left" -- raised there, the caller would read
    // lrdata one pass stale.
    reg              fin;
    assign busy = run || fin;
    assign done = fin;

    wire [LANES-1:0] nxt_todo;

    // ---- the resolver ---------------------------------------------------
    // One lane per bank per pass, and the LOWEST outstanding lane wins so the
    // forward-progress argument above holds.
    wire [NBW-1:0] lbank [0:LANES-1];
    wire [BW-1:0]  lrow  [0:LANES-1];
    genvar L;
    generate
    for (L = 0; L < LANES; L = L + 1) begin : g_split
        assign lbank[L] = (NBS > 0) ? laddr[AW*L +: NBW] : {NBW{1'b0}};
        assign lrow[L]  = laddr[AW*L + NBS +: BW];
    end
    endgenerate

    reg [LNW-1:0]    sel  [0:NB-1];           // which lane each bank serves
    reg [NB-1:0]     selv;                    // ... and whether it serves one
    reg [LANES-1:0]  served;
    integer b, l;
    always @(*) begin
        for (b = 0; b < NB; b = b + 1) begin
            sel[b]  = {LNW{1'b0}};
            selv[b] = 1'b0;
            // Descending, so the LOWEST matching lane is the one left assigned.
            for (l = LANES - 1; l >= 0; l = l - 1) begin
                if (todo[l] && ((NBS == 0) || (lbank[l] == b[NBW-1:0]))) begin
                    sel[b]  = l[LNW-1:0];
                    selv[b] = 1'b1;
                end
            end
        end
        for (l = 0; l < LANES; l = l + 1) begin
            served[l] = todo[l] && selv[lbank[l]] && (sel[lbank[l]] == l[LNW-1:0]);
        end
    end

    assign nxt_todo = todo & ~served;

    // ---- the banks -------------------------------------------------------
    wire [31:0] b_rdata [0:NB-1];
    reg  [BW-1:0]  b_addr_r [0:NB-1];
    reg  [31:0]    b_wdata_r [0:NB-1];
    reg  [3:0]     b_we_r [0:NB-1];
    integer k;
    always @(*) begin
        for (k = 0; k < NB; k = k + 1) begin
            b_addr_r[k]  = lrow[sel[k]];
            b_wdata_r[k] = lwdata[32*sel[k] +: 32];
            b_we_r[k]    = (run && !ph && we && selv[k]) ? 4'hF : 4'd0;
        end
    end

    genvar K;
    generate
    for (K = 0; K < NB; K = K + 1) begin : g_bank
        // The window writes LINEAR addresses; the interleave is decoded here, so
        // a CU_DATA burst is unchanged by this block existing.
        wire        a_hit = a_en
                          && ((NBS == 0) || (a_addr[NBW-1:0] == K[NBW-1:0]));
        rv_spad #(.WORDS(WORDS / NB), .MEM_PRIM(MEM_PRIM)) u_bank (
            .clk(clk),
            .a_en(a_hit), .a_we(a_we), .a_addr(a_addr[AW-1:NBS]),
            .a_wdata(a_wdata), .a_rdata(),
            .b_addr(b_addr_r[K]), .b_we(b_we_r[K]), .b_wdata(b_wdata_r[K]),
            .b_rdata(b_rdata[K])
        );
    end
    endgenerate

    // ---- the sequencer ---------------------------------------------------
    // Two cycles a pass because the banks register their address: drive, then
    // take. A read lands in the lane that ASKED for it, which is the crossbar
    // on the way back out and the other half of what this block costs.
    reg [LANES-1:0] served_q;

    always @(posedge clk) begin
        if (!resetn) begin
            run      <= 1'b0;
            ph       <= 1'b0;
            fin      <= 1'b0;
            todo     <= {LANES{1'b0}};
            served_q <= {LANES{1'b0}};
            pass_ctr <= 32'd0;
        end else begin
            served_q <= (run && !ph) ? served : {LANES{1'b0}};
            fin      <= 1'b0;

            if (!run) begin
                if (req && !fin && (lmask != {LANES{1'b0}})) begin
                    run  <= 1'b1;
                    ph   <= 1'b0;
                    todo <= lmask;
                end
            end else if (!ph) begin
                ph       <= 1'b1;
                pass_ctr <= pass_ctr + 32'd1;
            end else begin
                ph   <= 1'b0;
                todo <= nxt_todo;
                if (nxt_todo == {LANES{1'b0}}) begin
                    run <= 1'b0;
                    fin <= 1'b1;
                end
            end
        end
    end

    // The return crossbar: bank k's word goes to the lane that selected it.
    integer m;
    always @(posedge clk) begin
        for (m = 0; m < LANES; m = m + 1) begin
            if (served_q[m]) begin
                lrdata[32*m +: 32] <= b_rdata[lbank[m]];
            end
        end
    end

    // A bank count that does not divide the word count leaves a bank short, and
    // one above LANES has banks no lane can ever address.
    generate
    if ((BANKS > 0)
        && (((WORDS / BANKS) * BANKS != WORDS) || (BANKS > LANES)))
    begin : g_bad_nb
        kht_lds_requires_BANKS_to_divide_WORDS_and_not_exceed_LANES u_bad ();
    end
    if (BANKS < -1) begin : g_bad_nbn
        kht_lds_BANKS_below_minus_one_is_not_a_width u_bad ();
    end
    endgenerate

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (resetn && run && !ph && (served == {LANES{1'b0}})) begin
            $display("%0t ERROR kht_lds: a pass served no lane -- the resolver has lost forward progress, todo=%b",
                     $time, todo);
        end
    end
`endif

endmodule

`default_nettype wire
