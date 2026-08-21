// rv_l1 -- the internal L1: the PE's private, tagged, write-back cache over
// global DRAM. Direct mapped, 32-byte lines, one outstanding blocking miss.
//
// IT IS NEVER WRITTEN FROM OUTSIDE, and that single property is what removes
// coherence from this design entirely. Everything a peer can write lands in the
// external windows, which are the HOME of their addresses and carry no tags;
// everything in here is a copy of DRAM that only this core has touched. There
// is no external-write-versus-dirty-line case to lose sleep over because there
// is no way to construct one.
//
// A LINE IS ONE FLIT. 32 bytes is one MEM_RD_RESP payload and one MEM_WR_DATA
// beat, so a fill is one request and one response and a writeback is one
// descriptor and one beat. That is the protocol-adaptation half of the cache's
// job (design note s6.1) and it holds whatever the hit rate turns out to be.
//
// Lines are walked as eight 32-bit words on the array's second port. See
// rv_ram_be's header for why the array is 32 bits and not 256: eight cycles
// against a DRAM round trip is free, and a 256-bit CPU-side read is not.
//
// PER-LINE STATE LIVES IN THE TAG ARRAY. {valid, dirty, tag} is one LUTRAM
// word, so a line costs no flops and no indexed mux, and the line count stops
// costing LUT -- as flop arrays, valid and dirty were the whole reason 128
// lines cost 701 LUT more than 64, and `valid[idx]` was the start of the
// binding path. Two things follow: the array has no reset, so a power-on sweep
// clears it before the first access; and invalidate-all is a sweep of one line
// per cycle, which is why a store to CTL_INVAL blocks like CTL_FLUSH.
//
// FLUSH-ALL WAITS FOR ACKNOWLEDGEMENTS, not for the last beat to be sent. The
// DRAM hand-off in docs/arch/pe/programming.md is flush, doorbell, invalidate,
// read -- and the doorbell goes to the peer while the writebacks go to the
// memory agent, two destinations with no ordering between them. Only the write
// acknowledgement says the data is in memory, so the flush is not finished
// until every one has come back.

`default_nettype none

module rv_l1 #(
    parameter integer LINES    = 64,            // power of two; 32 B each
    parameter         MEM_PRIM = "block"
)(
    input  wire         clk,
    input  wire         resetn,

    // ---- CPU side ----
    input  wire [31:0]  probe_addr,   // EX-stage address; picks the tag to read
    input  wire         req,          // a global access is in MEM this cycle
    input  wire         we,
    input  wire [3:0]   be,
    input  wire [31:0]  addr,         // the MEM-stage address
    input  wire [31:0]  wdata,
    output wire [31:0]  rdata,        // valid in WB
    output wire         stall,

    input  wire         flush,        // pulse: write every dirty line back
    input  wire         inval,        // pulse: drop every line, dirty included
    output wire         flush_busy,

    // ---- NoC requestor side ----
    output reg          fill_valid,
    input  wire         fill_ready,
    output reg  [30:0]  fill_addr,
    input  wire         resp_valid,
    input  wire [255:0] resp_data,

    output reg          wb_valid,
    input  wire         wb_ready,
    output reg  [30:0]  wb_addr,
    output wire [255:0] wb_data,
    input  wire         wr_idle       // no writes outstanding at the requestor
);
    localparam integer IDX_W = $clog2(LINES);
    localparam integer TAG_W = 31 - 5 - IDX_W;
    // ONE WORD PER LINE: {valid, dirty, tag}. As flop arrays they cost the line
    // count twice, in flops and in the mux that reads one, and `valid[idx]` was
    // the START of the iteration-1 binding path.
    localparam integer TW = TAG_W + 2;

    wire [IDX_W-1:0] idx = addr[IDX_W+4:5];
    wire [TAG_W-1:0] tag = addr[30:IDX_W+5];

    // Declared before anything reads them: xvlog rejects a use-before-declare
    // that synthesis had accepted silently.
    localparam [3:0] L_IDLE = 4'd0, L_EV_RD = 4'd1, L_EV_SEND = 4'd2,
                     L_F_REQ = 4'd3, L_F_WAIT = 4'd4, L_F_WR  = 4'd5,
                     L_S_SCAN = 4'd6, L_S_RD  = 4'd7, L_S_SEND = 4'd8,
                     L_S_DRAIN = 4'd9, L_S_TEST = 4'd10, L_I_SCAN = 4'd11,
                     L_REPROBE = 4'd12;
    reg [3:0] st;

    reg [2:0]        wcnt;      // word being addressed within a line
    reg [2:0]        wcnt_d;    // ... and the one whose data is out now
    reg              wvalid_d;
    reg [IDX_W-1:0]  vic;       // line being evicted or swept
    reg [TAG_W-1:0]  vic_tag;
    reg [IDX_W:0]    scan;      // sweep cursor; one bit wider so it can end
    reg [255:0]      linebuf;

    wire [TW-1:0]    tt_q;
    wire             v_q   = tt_q[TW-1];
    wire             d_q   = tt_q[TW-2];
    wire [TAG_W-1:0] tag_q = tt_q[TAG_W-1:0];
    reg              tag_we;
    reg  [IDX_W-1:0] tag_wa;
    reg  [TW-1:0]    tag_wd;

    wire hit  = v_q && (tag_q == tag);
    wire miss = req && !hit;

    assign stall      = (st != L_IDLE) || miss;
    // Every state that owns the tag port's address. L_I_SCAN is in it so the
    // MEM stage holds for invalidate-all the way it already does for flush.
    assign flush_busy = (st == L_S_SCAN) || (st == L_S_TEST) || (st == L_S_RD) ||
                        (st == L_S_SEND) || (st == L_S_DRAIN) || (st == L_I_SCAN);
    assign wb_data    = linebuf;

    // A store hit dirties its line by WRITING this array, whose read port is
    // read_first: a probe of that index in the same cycle reads the old bit.
    wire            st_dirty = req && we && hit && !stall;
    reg             dw_v;
    reg [IDX_W-1:0] dw_idx;
    wire            d_eff = d_q || (dw_v && (dw_idx == idx));

    // Three claimants on the one tag read port, in priority order: the flush
    // sweep, the access that is waiting, and the access coming down from EX.
    // Leaving the sweep out of this reads the tag of whatever address happened
    // to be in MEM and writes a dirty line to that address instead.
    //
    // `stall && req`, not `stall`: a stall with no access in MEM is a control
    // store blocking, and re-reading ITS index would leave the tag port on a
    // dead address for the whole of it.
    wire [IDX_W-1:0] p_idx = flush_busy    ? scan[IDX_W-1:0]
                           : (stall && req) ? idx
                                            : probe_addr[IDX_W+4:5];

    kohaku_sdpram #(.WIDTH(TW), .DEPTH(LINES),
                    .MEM_PRIM("distributed"), .READ_LAT(1)) u_tag (
        .clk(clk),
        .wr_en(tag_we), .wr_addr(tag_wa), .wr_data(tag_wd),
        .rd_en(1'b1), .rd_addr(p_idx), .rd_data(tt_q)
    );

    // ---- the data array ---------------------------------------------------
    wire        a_walk = (st == L_EV_RD) || (st == L_F_WR) || (st == L_S_RD);
    wire        a_wr   = (st == L_F_WR);
    wire [31:0] a_rd;

    // XPORT_OK: the fill collides with the stalled access's own word once per
    // fill, and that read is DISCARDED, not bypassed. Guarded at the bottom.
    rv_ram_be #(.WORDS(LINES * 8), .MEM_PRIM(MEM_PRIM), .XPORT_OK(1)) u_data (
        .clk(clk),
        .a_en(a_walk),
        .a_we(a_wr ? 4'hF : 4'd0),
        .a_addr({(st == L_S_RD) ? scan[IDX_W-1:0] : vic, wcnt}),
        // ALWAYS THE BOTTOM WORD: `linebuf` rotates rather than being indexed,
        // which is what removes an 8:1 32-bit mux from this port.
        .a_wdata(linebuf[31:0]),
        .a_rdata(a_rd),
        .b_en(1'b1),
        // A store commits only on the cycle the access actually completes, so
        // a hit under a stall cannot write twice.
        .b_we((req && we && hit && !stall) ? be : 4'd0),
        .b_addr(addr[IDX_W+4:2]),
        .b_wdata(wdata),
        .b_rdata(rdata)
    );

    always @(posedge clk) begin
        if (!resetn) begin
            // NOT L_IDLE. The tag array is LUTRAM and has no reset, so every
            // line reads back undefined until it has been written once.
            st         <= L_I_SCAN;
            fill_valid <= 1'b0;
            wb_valid   <= 1'b0;
            wcnt       <= 3'd0;
            wcnt_d     <= 3'd0;
            wvalid_d   <= 1'b0;
            scan       <= {(IDX_W+1){1'b0}};
            tag_we     <= 1'b0;
            dw_v       <= 1'b0;
        end else begin
            tag_we   <= 1'b0;
            wcnt_d   <= wcnt;
            wvalid_d <= a_walk && !a_wr;
            dw_v     <= st_dirty;
            dw_idx   <= idx;

            // Eight of these leave word 0 in the bottom. `a_walk` too, because
            // `wvalid_d` is a cycle late by construction and is still set after
            // the walk ends -- a ninth rotation shifts the writeback by a word.
            if (wvalid_d && a_walk) linebuf <= {a_rd, linebuf[255:32]};

            // Exclusive with every write the case below makes: those all need
            // st != L_IDLE, and a store that commits needs !stall.
            if (st_dirty) begin
                tag_we <= 1'b1;
                tag_wa <= idx;
                tag_wd <= {2'b11, tag};
            end

            case (st)
            L_IDLE: begin
                wcnt <= 3'd0;
                scan <= {(IDX_W+1){1'b0}};
                if (flush)      st <= L_S_SCAN;
                else if (inval) st <= L_I_SCAN;
                else if (miss) begin
                    vic     <= idx;
                    vic_tag <= tag_q;
                    if (v_q && d_eff) st <= L_EV_RD;
                    else begin
                        fill_addr  <= {tag, idx, 5'd0};
                        fill_valid <= 1'b1;
                        st <= L_F_REQ;
                    end
                end
            end

            // Eight addresses out, eight words in one cycle behind; the last
            // word is in `linebuf` one cycle after the last address.
            L_EV_RD: if (wcnt == 3'd7) begin
                if (wvalid_d && (wcnt_d == 3'd7)) begin
                    wb_addr  <= {vic_tag, vic, 5'd0};
                    wb_valid <= 1'b1;
                    st <= L_EV_SEND;
                end
            end else wcnt <= wcnt + 3'd1;

            // No dirty clear: `vic` is `idx`, so the fill below rewrites this
            // line's whole word anyway.
            L_EV_SEND: if (wb_ready) begin
                wb_valid   <= 1'b0;
                fill_addr  <= {tag, idx, 5'd0};
                fill_valid <= 1'b1;
                wcnt       <= 3'd0;
                st <= L_F_REQ;
            end

            L_F_REQ: if (fill_ready) begin
                fill_valid <= 1'b0;
                st <= L_F_WAIT;
            end

            L_F_WAIT: if (resp_valid) begin
                linebuf    <= resp_data;
                tag_we     <= 1'b1;
                tag_wa     <= vic;
                tag_wd     <= {2'b10, tag};      // valid, clean
                wcnt       <= 3'd0;
                st <= L_F_WR;
            end

            // The last word commits at the edge that leaves this state, so the
            // access sees it on its next read -- one cycle later, never the
            // same edge, which is the port A / port B collision.
            L_F_WR: begin
                // Eight rotations return the line to the order it arrived in.
                linebuf <= {linebuf[31:0], linebuf[255:32]};
                if (wcnt == 3'd7) st <= L_IDLE;
                else wcnt <= wcnt + 3'd1;
            end

            // ---- flush-all ----
            // Two states per line, not one: this state presents `scan` on the
            // tag port and the word is only out in the next.
            L_S_SCAN: begin
                wcnt <= 3'd0;
                if (scan[IDX_W]) st <= L_S_DRAIN;
                else st <= L_S_TEST;
            end

            // `dirty` alone: a fill leaves a line valid and clean and only a
            // store hit sets dirty, so dirty implies valid by construction.
            L_S_TEST: if (d_q) st <= L_S_RD;
                      else begin
                          scan <= scan + 1'b1;
                          st   <= L_S_SCAN;
                      end

            L_S_RD: if (wcnt == 3'd7) begin
                if (wvalid_d && (wcnt_d == 3'd7)) begin
                    wb_addr  <= {tag_q, scan[IDX_W-1:0], 5'd0};
                    wb_valid <= 1'b1;
                    st <= L_S_SEND;
                end
            end else wcnt <= wcnt + 3'd1;

            L_S_SEND: if (wb_ready) begin
                wb_valid <= 1'b0;
                // Still valid, no longer dirty. The tag has to be carried back
                // in: this array holds one word per line, not three fields.
                tag_we   <= 1'b1;
                tag_wa   <= scan[IDX_W-1:0];
                tag_wd   <= {2'b10, tag_q};
                scan     <= scan + 1'b1;
                st <= L_S_SCAN;
            end

            L_S_DRAIN: if (wr_idle) st <= L_REPROBE;

            // Invalidate-all, and the power-on sweep the array's missing reset
            // needs. One line a cycle, and `stall` holds MEM for all of it.
            L_I_SCAN: if (scan[IDX_W]) st <= L_REPROBE;
                      else begin
                          tag_we <= 1'b1;
                          tag_wa <= scan[IDX_W-1:0];
                          tag_wd <= {TW{1'b0}};
                          scan   <= scan + 1'b1;
                      end

            // A SWEEP LEAVES THE TAG PORT ON ITS OWN CURSOR, so deciding hit or
            // miss the cycle it ends would evict to the sweep's address.
            L_REPROBE: st <= L_IDLE;

            default: st <= L_IDLE;
            endcase
        end
    end

`ifndef SYNTHESIS
    // An access may sit in MEM through a whole sweep, but it must never be
    // DECIDED on a word the sweep's cursor read. Deleting L_REPROBE fails here
    // rather than corrupting a writeback address.
    reg sweep_q;
    always @(posedge clk) sweep_q <= flush_busy;

    always @(posedge clk) begin
        if (resetn && sweep_q && req && !stall)
            $display("%0t ERROR rv_l1: an access completed on the sweep's tag read -- hit, miss and the victim tag are all wrong",
                     $time);
        // Acted on in L_IDLE only, which the MEM stage guarantees by not
        // committing the store that raises one while an access is outstanding.
        if (resetn && (flush || inval) && (st != L_IDLE))
            $display("%0t ERROR rv_l1: flush/inval raised in state %0d -- the sweep is dropped",
                     $time, st);
        // What u_data's XPORT_OK(1) promises: a fill word may collide with the
        // access's read, but never on a cycle that access completes on.
        if (resetn && a_wr && ({vic, wcnt} == addr[IDX_W+4:2]) && req && !stall)
            $display("%0t ERROR rv_l1: word %0d completed an access on the cycle the fill wrote it -- the load data is undefined",
                     $time, addr[IDX_W+4:2]);
    end
`endif

endmodule

`default_nettype wire
