// rv64_l1 -- the private write-back L1 over the node's cached range.
//
// Direct mapped, 32-byte lines, one outstanding miss. Physically tagged: it sits
// BEHIND the TLB, so nothing here ever sees a virtual address and an ASID change
// costs it nothing.
//
// IT IS NEVER WRITTEN FROM OUTSIDE, and that single property removes coherence
// from the design. Everything a peer can write lands in the node windows, which
// are the HOME of their addresses and are uncached here.
//
// A LINE IS ONE BEAT. 32 bytes is one 256-bit AXI beat and one flit payload, so
// a fill is one request and one response and a writeback is one descriptor and
// one beat.
//
// PER-LINE STATE LIVES IN THE TAG ARRAY. {valid, dirty, tag} is one LUTRAM word,
// so a line costs no flops and no indexed mux. On the RV32 PE, valid and dirty
// as flop arrays were the whole reason 128 lines cost 701 LUT more than 64.
//
// ONE OUTSTANDING MISS, WHICH DESIGN s9 RESOLVED AGAINST -- and the reason it is
// safe here is that the deadlock s5 raised has been removed rather than
// tolerated. That deadlock is a page-table walk issued BY an L1 miss and
// needing the same port. Page tables live in staging (design s8), staging is
// outside the cached range, so a walk never enters this module and cannot wait
// on the miss that caused it. What s9's non-blocking L1 additionally buys is
// hit-under-miss for ordinary code; that is a performance upgrade, and the
// miss-status file it needs is not free at the budget derived in STATE.md.

`default_nettype none

module rv64_l1 #(
    parameter integer LINES    = 64,          // power of two; 32 B each
    parameter integer ADDR_W   = 40,
    parameter         MEM_PRIM = "block"
)(
    input  wire                clk,
    input  wire                resetn,

    // E-stage address, so the tag read is issued a cycle before the access.
    input  wire [ADDR_W-1:0]   probe_addr,
    input  wire                req,
    input  wire                we,
    input  wire [7:0]          be,
    input  wire [ADDR_W-1:0]   addr,
    input  wire [63:0]         wdata,
    output wire [63:0]         rdata,
    output wire                stall,

    input  wire                flush,        // pulse: write back dirty lines
    input  wire                inval,        // pulse: drop every line, dirty too
    output wire                flush_busy,

    output reg                 fill_valid,
    input  wire                fill_ready,
    output reg  [ADDR_W-6:0]   fill_addr,    // line address
    input  wire                resp_valid,
    input  wire [255:0]        resp_data,

    output reg                 wb_valid,
    input  wire                wb_ready,
    output reg  [ADDR_W-6:0]   wb_addr,
    output wire [255:0]        wb_data,
    input  wire                wr_idle,      // no writes outstanding upstream

    output wire                dbg_hit,      // a hit retiring this cycle
    output wire                dbg_miss,     // a confirmed miss starting a fill
    output wire                dbg_wr,       // a store landing in the array
    output wire                dbg_evrd,     // in the eviction read loop
    output wire [63:0]         dbg_ard,      // what port A is returning
    output wire [7:0]          dbg_st
);
    localparam integer IDX_W = $clog2(LINES);
    localparam integer TAG_W = ADDR_W - 5 - IDX_W;
    localparam integer TW    = TAG_W + 2;

    wire [IDX_W-1:0] idx = addr[IDX_W+4:5];
    wire [TAG_W-1:0] tag = addr[ADDR_W-1:IDX_W+5];

    localparam [3:0] L_IDLE = 4'd0, L_EV_RD = 4'd1, L_EV_SEND = 4'd2;
    localparam [3:0] L_F_REQ = 4'd3, L_F_WAIT = 4'd4, L_F_WR = 4'd5;
    localparam [3:0] L_S_SCAN = 4'd6, L_S_RD = 4'd7, L_S_SEND = 4'd8;
    localparam [3:0] L_S_DRAIN = 4'd9, L_S_TEST = 4'd10, L_I_SCAN = 4'd11;
    localparam [3:0] L_REPROBE = 4'd12, L_REPROBE2 = 4'd13;
    reg [3:0] st;

    reg [1:0]        wcnt;        // word being ADDRESSED within a line
    reg [2:0]        got;         // words collected, a cycle behind (L_EV_RD)
    reg [1:0]        wcnt_d;
    reg              wvalid_d;
    reg [IDX_W-1:0]  vic;
    reg [TAG_W-1:0]  vic_tag;
    reg [IDX_W:0]    scan;
    reg [255:0]      linebuf;

    wire [TW-1:0]    tt_q;
    wire             v_q   = tt_q[TW-1];
    wire             d_q   = tt_q[TW-2];
    wire [TAG_W-1:0] tag_q = tt_q[TAG_W-1:0];
    reg              tag_we;
    reg  [IDX_W-1:0] tag_wa;
    reg  [TW-1:0]    tag_wd;

    // THE TAG AND THE DATA BOTH ANSWER A CYCLE AFTER THE ADDRESS. `probe_addr`
    // here is the same cycle as `addr`, not a stage ahead as on the RV32 PE, so
    // on the first cycle of an access `tt_q` still holds the PREVIOUS index's
    // entry -- and for sequential addresses the neighbouring line carries the
    // same tag, so it reports a hit, skips the fill, and returns the previous
    // address's data. Every access therefore holds for one cycle first.
    reg [ADDR_W-1:0] a_q;
    reg              a_v;
    always @(posedge clk) begin
        a_q <= addr;
        a_v <= req;
    end
    // A READ IMMEDIATELY AFTER A WRITE NEEDS AN EXTRA CYCLE. The array is
    // `no_change`, so port B's read output HOLDS while that port is writing --
    // sampling it the next cycle returns the pre-write value. The tell is a
    // check that fails while printing the value it wanted.
    reg wrote_q;
    always @(posedge clk) begin
        wrote_q <= (req && we && hit && !stall);
    end

    wire fresh = a_v && (a_q == addr) && !wrote_q;

    wire hit  = fresh && v_q && (tag_q == tag);
    // A REAL miss, with the tag known. Acting on `!hit` alone starts a fill in
    // the stale cycle, capturing the wrong victim tag and the wrong dirty bit.
    wire miss = req && fresh && !hit;

    // Holds on anything that is not a confirmed hit, which includes the stale
    // first cycle -- `miss` above is narrower on purpose.
    assign stall      = (st != L_IDLE) || (req && !hit);
    assign flush_busy = (
        (st == L_S_SCAN)
        || (st == L_S_TEST)
        || (st == L_S_RD)
        || (st == L_S_SEND)
        || (st == L_S_DRAIN)
        || (st == L_I_SCAN)
    );
    assign wb_data    = linebuf;

    // A store hit dirties its line by WRITING this array, whose read port is
    // read-first: a probe of that index in the same cycle reads the old bit.
    wire            st_dirty = req && we && hit && !stall;
    reg             dw_v;
    reg [IDX_W-1:0] dw_idx;
    wire            d_eff = d_q || (dw_v && (dw_idx == idx));

    // Three claimants on the one tag read port, in priority order: the sweep,
    // the access that is waiting, and the access coming down from E. Leaving the
    // sweep out reads the tag of whatever happened to be in M and writes a dirty
    // line to that address instead.
    wire [IDX_W-1:0] p_idx = flush_busy     ? scan[IDX_W-1:0]
                           : (stall && req) ? idx
                                            : probe_addr[IDX_W+4:5];

    kohaku_sdpram #(.WIDTH(TW), .DEPTH(LINES),
                    .MEM_PRIM("distributed"), .READ_LAT(1)) u_tag (
        .clk(clk),
        .wr_en(tag_we), .wr_addr(tag_wa), .wr_data(tag_wd),
        .rd_en(1'b1), .rd_addr(p_idx), .rd_data(tt_q)
    );

    // ---- the data array, 64-bit words ---------------------------------------
    wire        a_walk = (st == L_EV_RD) || (st == L_F_WR) || (st == L_S_RD);
    wire        a_wr   = (st == L_F_WR);
    wire [63:0] a_rd;
    wire [63:0] b_rd;

    wire [IDX_W+1:0] a_addr = {(st == L_S_RD) ? scan[IDX_W-1:0] : vic, wcnt};
    wire [IDX_W+1:0] b_addr = {idx, addr[4:3]};

    // ALWAYS THE BOTTOM WORD: `linebuf` rotates rather than being indexed, which
    // removes a 4:1 64-bit mux from this port.
    // XPORT_OK: the fill collides with the stalled access's own word once per
    // fill, and that read is DISCARDED -- the value is taken after L_REPROBE,
    // when port A is idle -- not bypassed.
    rv64_ram_be #(.WORDS(LINES * 4), .MEM_PRIM(MEM_PRIM), .XPORT_OK(1)) u_data (
        .clk(clk),
        .a_en(a_walk), .a_we(a_wr ? 8'hFF : 8'd0),
        .a_addr(a_addr), .a_wdata(linebuf[63:0]), .a_rdata(a_rd),
        .b_en(1'b1), .b_we((req && we && hit && !stall) ? be : 8'd0),
        .b_addr(b_addr), .b_wdata(wdata), .b_rdata(b_rd)
    );

    assign rdata   = b_rd;
    assign dbg_hit = req && hit && (st == L_IDLE);
    assign dbg_miss= (st == L_IDLE) && miss;
    assign dbg_wr  = req && we && hit && !stall;
    assign dbg_evrd= (st == L_EV_RD);
    assign dbg_ard = linebuf[63:0];
    assign dbg_st  = {1'b0, got, st};

    // ---- the machine --------------------------------------------------------
    reg flush_pend, inval_pend;

    always @(posedge clk) begin
        if (!resetn) begin
            st         <= L_S_SCAN;      // power-on sweep: no array reset
            scan       <= {(IDX_W+1){1'b0}};
            fill_valid <= 1'b0;
            wb_valid   <= 1'b0;
            tag_we     <= 1'b0;
            dw_v       <= 1'b0;
            flush_pend <= 1'b0;
            inval_pend <= 1'b1;          // ...as an invalidate, not a flush
            wvalid_d   <= 1'b0;
        end
        else begin
            tag_we   <= 1'b0;
            dw_v     <= st_dirty;
            dw_idx   <= idx;
            wcnt_d   <= wcnt;
            wvalid_d <= a_walk && !a_wr;

            if (flush) begin
                flush_pend <= 1'b1;
            end
            if (inval) begin
                inval_pend <= 1'b1;
            end

            // A hit that stores marks the line dirty.
            if (st_dirty) begin
                tag_we <= 1'b1;
                tag_wa <= idx;
                tag_wd <= {1'b1, 1'b1, tag};
            end

            case (st)
                L_IDLE: begin
                    if (flush_pend || inval_pend) begin
                        scan <= {(IDX_W+1){1'b0}};
                        st   <= inval_pend && !flush_pend ? L_I_SCAN : L_S_SCAN;
                    end
                    else if (miss) begin
                        vic     <= idx;
                        vic_tag <= tag_q;
                        wcnt    <= 2'd0;
                        got     <= 3'd0;
                        // Dirty and valid means it has to leave before the fill.
                        st <= (v_q && d_eff) ? L_EV_RD : L_F_REQ;
                    end
                end

                // ---- eviction ----
                // THE ADDRESS LEADS THE DATA BY A CYCLE. Exiting on the address
                // counter sends the line one word short and rotated wrong; the
                // exit has to be on words COLLECTED.
                L_EV_RD: begin
                    if (wvalid_d) begin
                        linebuf <= {a_rd, linebuf[255:64]};
                        got     <= got + 3'd1;
                    end
                    if (wcnt != 2'd3) begin
                        wcnt <= wcnt + 2'd1;
                    end
                    if (wvalid_d && (got == 3'd3)) begin
                        wb_addr  <= {vic_tag, vic};
                        wb_valid <= 1'b1;
                        st       <= L_EV_SEND;
                    end
                end
                L_EV_SEND: if (wb_ready) begin
                    wb_valid <= 1'b0;
                    st       <= L_F_REQ;
                end

                // ---- fill ----
                // `fill_valid && fill_ready`, NOT `fill_ready` alone: the port's
                // ready is an idle indicator and is already high on entry, so
                // testing it alone clears the request in the same cycle it is
                // raised and the port never sees it.
                L_F_REQ: begin
                    fill_addr  <= addr[ADDR_W-1:5];
                    fill_valid <= 1'b1;
                    if (fill_valid && fill_ready) begin
                        fill_valid <= 1'b0;
                        st         <= L_F_WAIT;
                    end
                end
                L_F_WAIT: if (resp_valid) begin
                    linebuf <= resp_data;
                    wcnt    <= 2'd0;
                    st      <= L_F_WR;
                end
                L_F_WR: begin
                    linebuf <= {linebuf[63:0], linebuf[255:64]};
                    if (wcnt == 2'd3) begin
                        tag_we <= 1'b1;
                        tag_wa <= vic;
                        tag_wd <= {1'b1, 1'b0, addr[ADDR_W-1:IDX_W+5]};
                        st     <= L_REPROBE;
                    end
                    wcnt <= wcnt + 2'd1;
                end
                // TWO cycles, not one. `tag_we` is itself a register, so the
                // write lands a cycle after the state sets it, and the array is
                // read-first -- with one cycle L_IDLE still sees the OLD tag,
                // misses again, and evicts the line it just filled.
                L_REPROBE:  st <= L_REPROBE2;
                L_REPROBE2: st <= L_IDLE;

                // ---- flush sweep ----
                L_S_SCAN: st <= L_S_TEST;
                L_S_TEST: begin
                    if (scan[IDX_W]) begin
                        st <= L_S_DRAIN;
                    end
                    else if (v_q && d_q) begin
                        vic     <= scan[IDX_W-1:0];
                        vic_tag <= tag_q;
                        wcnt    <= 2'd0;
                        got     <= 3'd0;
                        st      <= L_S_RD;
                    end
                    else begin
                        scan <= scan + 1'b1;
                        st   <= L_S_SCAN;
                    end
                end
                L_S_RD: begin
                    if (wvalid_d) begin
                        linebuf <= {a_rd, linebuf[255:64]};
                        got     <= got + 3'd1;
                    end
                    if (wcnt != 2'd3) begin
                        wcnt <= wcnt + 2'd1;
                    end
                    if (wvalid_d && (got == 3'd3)) begin
                        wb_addr  <= {vic_tag, vic};
                        wb_valid <= 1'b1;
                        st       <= L_S_SEND;
                    end
                end
                L_S_SEND: if (wb_ready) begin
                    wb_valid <= 1'b0;
                    tag_we   <= 1'b1;
                    tag_wa   <= vic;
                    tag_wd   <= {1'b1, 1'b0, vic_tag};   // clean, still valid
                    scan     <= scan + 1'b1;
                    st       <= L_S_SCAN;
                end
                // FLUSH FINISHES ON ACKNOWLEDGEMENTS, not on the last beat sent:
                // only the write response says the data is in memory.
                L_S_DRAIN: if (wr_idle) begin
                    flush_pend <= 1'b0;
                    if (inval_pend) begin
                        scan <= {(IDX_W+1){1'b0}};
                        st   <= L_I_SCAN;
                    end else begin
                        st <= L_IDLE;
                    end
                end

                // ---- invalidate sweep ----
                L_I_SCAN: begin
                    tag_we <= 1'b1;
                    tag_wa <= scan[IDX_W-1:0];
                    tag_wd <= {TW{1'b0}};
                    scan   <= scan + 1'b1;
                    if (scan[IDX_W-1:0] == {IDX_W{1'b1}}) begin
                        inval_pend <= 1'b0;
                        st         <= L_IDLE;
                    end
                end

                default: st <= L_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
