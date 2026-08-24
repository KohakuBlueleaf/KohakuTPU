// -----------------------------------------------------------------------------
// format-example2.v -- the house Verilog style, one construct at a time.
//
// Verilog-2001, every syntactic form the language has, each written in the shape
// this tree wants it in. Read it the way PEP-8's examples are read: the code is
// deliberately without purpose, the FORMATTING is the content.
//
// It parses. `xvlog -sv format-example2.v` is clean, so every shape here is one
// the tools accept and not just one that reads well.
//
// The comment density in this file is far above the tree's own budget on purpose
// -- here the commentary IS the deliverable. Nowhere else.
//
// THE RULES. Everything below is an instance of one of these.
//
//   F1  80 columns. A line that will not fit is an expression that wants a name.
//   F2  4 spaces of indent, never a tab.
//   F3  A declaration is ONE line. Several names on it are fine; a continuation
//       line is not.
//   F4  begin/end on every if / else / for / while / repeat / always body. The
//       single exception is a case item that is one simple statement.
//   F5  case items are indented one level inside `case`; `endcase` aligns with
//       `case`.
//   F6  An expression that fits on one line STAYS on one line. One that does not
//       is wrapped in its OWN parentheses, one term per line, each continuation
//       line LEADING with its operator rather than trailing it. A nested group
//       breaks the same way only when IT is complex -- too long for its line, or
//       carrying enough operators to be parsed rather than read. Every rule
//       below that breaks a line is shown BOTH ways, simple and complex.
//   F7  `generate` adds no indent level, and every `begin` inside one is named.
//   F8  One item per line in a port list, a parameter list and an instantiation.
//   F9  Comments go ABOVE the code they explain, at its indent.
//   F10 `<=` only under `@(posedge ...)`; `=` in `always @(*)`, in functions and
//       tasks, and in a bench's procedural code. Never both to one variable.
//   F11 Every literal wider than one bit is sized and based.
//   F12 Compiler directives start at column 0, whatever they are nested in.
// -----------------------------------------------------------------------------

// F12. `timescale and `default_nettype are the file's frame and they bracket
// everything. `default_nettype none is not style: without it a typo in a port
// name becomes a one-bit wire and the design still elaborates.
`timescale 1ns / 1ps
`default_nettype none


// ============================================================================
// 1. MACROS
// ============================================================================
// A macro is UPPER_CASE and carries its subsystem's prefix, because the
// preprocessor has ONE flat namespace for the whole compilation unit -- a
// `WIDTH defined in two files is a redefinition warning at best.
`define FMT_WORD_BITS 32

// Every argument is parenthesised, and so is the whole body. Without the outer
// pair the caller's operators re-associate the expansion; without the inner
// ones an argument that is itself an expression does.
`define FMT_MIN(a, b) (((a) < (b)) ? (a) : (b))

// A multi-line macro is the ONE place a trailing continuation is allowed,
// because the language offers no alternative. The backslashes are aligned so
// that a missing one is visible, and the body still follows F6.
`define FMT_CLAMP(v, lo, hi) (                                              \
    ((v) < (lo))   ? (lo)                                                   \
    : ((v) > (hi)) ? (hi)                                                   \
    : (v)                                                                   \
)

// A conditional-compilation block is bracketed at column 0 by F12, and it says
// what it selects on BOTH arms. `ifndef ... `else is harder to read than
// `ifdef ... `else, so the positive form is the one to write.
`ifdef FMT_DEBUG
`define FMT_TRACE(msg) $display("[fmt] %0t %s", $time, msg)
`else
`define FMT_TRACE(msg)
`endif


// ============================================================================
// 2. THE MODULE HEADER
// ============================================================================
// ANSI-2001 form, never the Verilog-95 split of a bare name list plus separate
// direction declarations: the 95 form states each port twice and the two halves
// drift.
//
// F8. One parameter per line and one port per line, each with its own comment
// where it needs one. The name column and the paren column are aligned inside a
// group and re-aligned freely across a blank line -- alignment is a local
// convenience, not a file-wide invariant, or every added port re-touches the
// whole header.
//
// Ports are grouped: clock and reset, then one group per interface, in the order
// data flows. A blank line between groups, never two.
module fmt_shapes #(
    // A width parameter is `integer` so it is not silently 32 bits of signed
    // when it reaches a comparison.
    parameter integer DATA_W    = 32,
    parameter integer ADDR_W    = 40,
    parameter integer PORTS     = 2,
    // A parameter used as a WIDTH must have a sized default. `= 0` types this
    // as a 32-bit signed integer, and a 688-bit override then truncates
    // silently -- only a sized literal gives it the width it is meant to have.
    parameter [63:0]  INIT_WORD = 64'h0000_0000_0000_0000,
    // Verilog-2001 signed parameters. State the signedness; do not rely on the
    // default, which is unsigned for a sized default and signed for a bare one.
    parameter signed [7:0] BIAS = -8'sd4,
    // A boolean parameter is 0/1 and named for what 1 means, so `HAS_L2 = 0`
    // reads correctly without looking anything up.
    parameter integer HAS_L2    = 1
) (
    input  wire                    clk,
    input  wire                    resetn,

    // One interface per group, in the order data flows through the block.
    input  wire                    s_valid,
    output wire                    s_ready,
    input  wire [DATA_W-1:0]       s_data,
    input  wire                    s_last,

    output wire                    m_valid,
    input  wire                    m_ready,
    output wire [DATA_W-1:0]       m_data,
    output wire                    m_last,

    // A vector of per-port signals is ONE flat port, not an unpacked array: an
    // unpacked array port is SystemVerilog, and this tree is Verilog-2001.
    output wire [PORTS*ADDR_W-1:0] p_addr,
    output wire [PORTS-1:0]        p_req,
    input  wire [PORTS-1:0]        p_gnt,

    output wire [3:0]              status,
    output wire                    busy
);

    // ========================================================================
    // 3. PARAMETERS AND LOCALPARAMS
    // ========================================================================
    // F3. A declaration is one line. Several names on it are fine, and grouping
    // related constants that way is better than one line each -- what is banned
    // is the continuation, because a hand-aligned block across three lines reads
    // as a table and diffs as a paragraph: adding a name in the middle
    // re-aligns every line, so the diff touches all of them and says nothing.
    localparam integer WORDS   = 1 << 10;
    localparam integer WSEL_W  = $clog2(WORDS);
    localparam integer PSEL_W  = (PORTS > 1) ? $clog2(PORTS) : 1;
    localparam integer BEATS   = DATA_W / 8;

    // A state encoding: one line per group of related states, `=` aligned
    // within the line only.
    localparam [2:0] S_IDLE = 3'd0, S_ADDR = 3'd1, S_DATA = 3'd2;
    localparam [2:0] S_RESP = 3'd3, S_DONE = 3'd4, S_FAULT = 3'd5;

    // A derived constant gets a name rather than being spelled out at each use,
    // and it is sized where the width is load-bearing.
    localparam [15:0] BEATS16 = BEATS[15:0];

    // Verilog-2001 arithmetic on parameters, including the power operator. Note
    // that `**` on `integer` operands is signed, which is why the exponent is
    // written as a plain decimal and never as a sized literal.
    localparam integer CAP = 2 ** 4;

    // ========================================================================
    // 4. LITERALS
    // ========================================================================
    // F11. Anything wider than one bit is sized AND based. An unsized literal
    // is 32 bits of signed integer, and that has cost this tree real debugging:
    // `{16{16'h3C00 + e * 8}}` promotes the inner expression to 32 bits because
    // `8` is unsized, so the replication built eight 32-bit words instead of
    // sixteen 16-bit ones.
    localparam [31:0] LIT_DEC = 32'd1234;
    localparam [31:0] LIT_HEX = 32'hDEAD_BEEF;
    localparam [7:0]  LIT_BIN = 8'b1010_0101;
    localparam [11:0] LIT_OCT = 12'o7070;

    // Underscores every four digits in anything long enough to miscount. They
    // are free -- the lexer drops them.
    localparam [39:0] LIT_ADDR = 40'h01_8000_0000;

    // A one-bit literal is the one exception: `1'b0` and `1'b1` are written
    // sized anyway, because `0` and `1` in a concatenation are 32 bits.
    localparam FLAG_ON = 1'b1;

    // x and z are sized too, and they appear only in a don't-care context --
    // a casez label, or a simulation-only default. Never in synthesised logic.
    localparam [3:0] LIT_DC = 4'bzzzz;

    // ========================================================================
    // 5. NETS AND VARIABLES
    // ========================================================================
    // Type, then width, then names, with the three columns aligned in a group.
    // `wire` and `reg` line up because the width bracket does.
    wire             go;
    wire             stall;
    wire [DATA_W-1:0] d_in;
    wire [PORTS-1:0]  sel_oh;

    reg  [2:0]        st;
    reg  [WSEL_W-1:0] wptr;
    reg  [DATA_W-1:0] hold_q;
    reg               busy_q;

    // Several names on one line when they are the same thing repeated -- a
    // pipeline's stages, a bank's per-lane copies.
    reg [DATA_W-1:0] p0_q, p1_q, p2_q;

    // An UNPACKED array is the memory shape, and its dimension goes after the
    // name. Declaring `[0:N-1]` rather than `[N-1:0]` is the convention here
    // because an unpacked dimension is an index space, not a bit field.
    reg [DATA_W-1:0] mem [0:WORDS-1];

    // Two dimensions when the second one is a real second axis. More than two
    // is a sign the shape wants a name of its own.
    reg [7:0] tile [0:3][0:3];

    // `integer` is a loop counter and a simulation variable, never a port and
    // never a datapath: it is 32 bits SIGNED, so `i - 1` on an integer at 0 is
    // -1 and not 4294967295, which is the opposite of what a `reg [31:0]` does.
    integer i;
    integer j;

    // `genvar` belongs to the generate loops below and is declared beside them,
    // not here, so its scope is visible at the loop.

    // Verilog-2001 has wand / wor / tri / trireg and strength specifications.
    // This tree uses NONE of them: a resolved net hides a driver conflict that
    // `default_nettype none` and a single-driver discipline would have caught.
    // `supply0` / `supply1` are the two that appear, and only in a wrapper that
    // has to tie a vendor primitive's pin.
    supply0 gnd;
    supply1 vdd;

    // ========================================================================
    // 6. CONTINUOUS ASSIGNMENT AND THE EXPRESSION RULES
    // ========================================================================
    // A short assignment is one line, and staying on one line is preferred to
    // wrapping. F1's 80 columns is the only thing that forces a wrap.
    assign go      = s_valid && s_ready;
    assign s_ready = !stall;
    assign d_in    = s_data;

    // F6. A multi-line BOOLEAN gets its own parentheses and one term per line,
    // each led by its operator. The outer parens are what make the shape
    // unambiguous to the reader -- and to the next person to add a term, who
    // adds a line rather than re-flowing four.
    assign stall = (
        !m_ready
        || (st == S_FAULT)
        || (wptr == {WSEL_W{1'b1}})
        || busy_q
    );

    // A NESTED group. The SIMPLE one stays on its continuation line: an inner
    // group is not a reason to break by itself, only length or operator count
    // is. F6 governs the outer expression; the inner one is just a term.
    wire hazard = (
        go
        // small line need no nested group
        && ((st == S_ADDR) || (st == S_DATA))
        && !stall
    );

    // The COMPLEX one earns its own block, and the rule is RECURSIVE and applied
    // PER TERM: each group breaks only if that group is itself too long or
    // operator-heavy. Below, the first arm fits and stays on one line while the
    // second does not and breaks -- the two shapes side by side in one group is
    // correct, not an inconsistency, because each arm was judged on its own.
    wire hazard_deep = (
        go
        && (
            ((st == S_ADDR) && p_gnt[0] && !s_last)
            || (
                (st == S_DATA)
                && (wptr != {WSEL_W{1'b0}})
                && s_last
            )
        )
        && !stall
    );

    // ...and at that point the terms want names instead, which is what F1's "an
    // expression that wants a name" means. Two levels of nesting is legal and it
    // is also the signal to stop nesting: prefer this whenever the terms have
    // names worth reading, which is nearly always.
    wire in_transfer  = (st == S_ADDR) || (st == S_DATA);
    wire hazard_named = go && in_transfer && !stall;

    wire addr_armed   = (st == S_ADDR) && p_gnt[0] && !s_last;
    wire data_armed   = (st == S_DATA) && (wptr != {WSEL_W{1'b0}}) && s_last;
    wire hazard_flat  = go && (addr_armed || data_armed) && !stall;

    // F6 for a TERNARY CHAIN. The `?` column is aligned across the arms, every
    // continuation line leads with its `:`, and the final arm is a bare `:`
    // with no `?` after it. The first arm carries no leading `:`.
    wire [2:0] next_st = (
        (st == S_IDLE)   ? (go ? S_ADDR : S_IDLE)
        : (st == S_ADDR) ? S_DATA
        : (st == S_DATA) ? (s_last ? S_RESP : S_DATA)
        : (st == S_RESP) ? S_DONE
        : S_IDLE
    );

    // A ternary that fits stays on one line. Wrapping a short one costs three
    // lines and buys nothing.
    wire [DATA_W-1:0] out_d = busy_q ? hold_q : d_in;

    // A CONCATENATION that fits is one line, and a shift-register concat is the
    // canonical short one.
    wire [3:0] shifted = {status[2:0], s_valid};

    // One that does not fit breaks one field per line, and each field is
    // commented with its bit position -- the braces take the role F6 gives the
    // parentheses. Field-per-line is also the right shape for a SHORT concat
    // whose fields need explaining, which is the one case where length is not
    // what decides.
    assign status = {
        busy_q,             // [3] a transfer is in flight
        (st == S_FAULT),    // [2] sticky, cleared by reset only
        p_gnt[0],           // [1]
        s_valid             // [0]
    };

    // A REPLICATION is `{N{expr}}` and the inner expression is sized
    // explicitly, for the reason F11 gives.
    wire [DATA_W-1:0] all_ones = {DATA_W{1'b1}};
    wire [DATA_W-1:0] splat    = {(DATA_W/8){8'hA5}};

    // Indexed part-select, Verilog-2001. `+:` with a variable base is the only
    // way to index a flat vector by a runtime value, and it is preferred to
    // arithmetic on both bounds -- `[32*i+31 : 32*i]` is not legal with a
    // variable `i` at all.
    wire [7:0] byte_of = d_in[8*wptr[1:0] +: 8];

    // A fixed slice is written with plain bounds and the arithmetic left
    // visible, so the field boundaries are readable.
    wire [15:0] lo_half = d_in[15:0];
    wire [15:0] hi_half = d_in[DATA_W-1 : DATA_W-16];

    // Signed arithmetic is explicit at the point of use. Verilog's rule is that
    // ONE unsigned operand makes the whole expression unsigned, so the cast
    // goes on both sides of a comparison or on neither.
    wire signed [DATA_W-1:0] sd = $signed(d_in);
    wire lt = ($signed(d_in) < $signed({{(DATA_W-8){BIAS[7]}}, BIAS}));

    // Reduction operators get their own line and a name. `|x` and `&x` are one
    // character and they are easy to misread as the binary form.
    wire any_set = |d_in;
    wire all_set = &d_in;
    wire parity  = ^d_in;

    // Precedence is stated with parentheses even where the defaults are right.
    // `a & b | c` is legal and it is also a bug report waiting to be filed.
    wire mixed = (any_set & all_set) | (parity ^ FLAG_ON);

    // A macro call reads as an expression and takes no special layout.
    wire [3:0] clamped = `FMT_CLAMP(wptr[3:0], 4'd2, 4'd9);

    // ========================================================================
    // 7. ATTRIBUTES
    // ========================================================================
    // A Verilog-2001 attribute sits on the line ABOVE what it applies to when
    // the line would otherwise not fit, and inline when it would. It always
    // carries a comment saying WHY, because an attribute is a measurement
    // result and the measurement is not in the code.
    // Replicated: this net drives every slice of the per-port mux, and at 267
    // loads the route was 70% of its own cone.
    (* max_fanout = 32 *) wire [PSEL_W-1:0] psel;

    // `keep` and `dont_touch` are the two that change synthesis behaviour, so
    // they name the report that justified them.
    (* keep = "true" *) wire probe_edge = go && !stall;

    // ========================================================================
    // 8. COMBINATIONAL always
    // ========================================================================
    // F4. `always` takes begin/end even when it holds one statement, and F10
    // makes every assignment inside a combinational block blocking.
    //
    // `@(*)` and not a hand-written sensitivity list: a list is a maintenance
    // hazard and an omission is a latch in synthesis and a stale value in
    // simulation, which is the worst pair of failure modes available.
    reg [PSEL_W-1:0] psel_r;
    always @(*) begin
        psel_r = {PSEL_W{1'b0}};
        for (i = PORTS - 1; i >= 0; i = i - 1) begin
            if (p_gnt[i]) begin
                psel_r = i[PSEL_W-1:0];
            end
        end
    end

    assign psel = psel_r;

    // A combinational block that can leave its output unassigned is a latch.
    // The default assignment on entry is what prevents it, and it goes FIRST
    // rather than in an `else`, so adding a branch cannot reintroduce the
    // problem.
    reg [DATA_W-1:0] mux_r;
    always @(*) begin
        mux_r = {DATA_W{1'b0}};
        if (go) begin
            mux_r = d_in;
        end else if (busy_q) begin
            mux_r = hold_q;
        end
    end

    // ========================================================================
    // 9. case, casez, casex
    // ========================================================================
    // F5. Items are indented one level inside `case`, and `endcase` aligns with
    // `case`. F4's ONE exception lives here: a case item that is a single
    // simple statement needs no begin/end.
    reg [3:0] enc_r;
    always @(*) begin
        case (st)
            S_IDLE:  enc_r = 4'h0;
            S_ADDR:  enc_r = 4'h1;
            S_DATA:  enc_r = 4'h2;
            S_RESP:  enc_r = 4'h3;
            S_DONE:  enc_r = 4'h4;
            default: enc_r = 4'hF;
        endcase
    end

    // Several labels on one item are comma-separated on one line, and wrap with
    // one label per line if they do not fit.
    reg active_r;
    always @(*) begin
        case (st)
            S_ADDR, S_DATA, S_RESP: active_r = 1'b1;
            default:                active_r = 1'b0;
        endcase
    end

    // As soon as ONE item needs more than a simple statement, EVERY item in
    // that case takes begin/end. Mixing the two shapes in one case is the thing
    // this rule exists to prevent -- the reader cannot see where an arm ends.
    reg [DATA_W-1:0] acc_r;
    reg              ovf_r;
    always @(*) begin
        acc_r = hold_q;
        ovf_r = 1'b0;
        case (st)
            S_DATA: begin
                acc_r = hold_q + d_in;
                if (acc_r < hold_q) begin
                    ovf_r = 1'b1;
                end
            end
            S_RESP: begin
                acc_r = {DATA_W{1'b0}};
            end
            default: begin
                acc_r = hold_q;
            end
        endcase
    end

    // A NESTED case indents like any other block, and the inner `endcase`
    // aligns with the inner `case`. Two levels is the practical limit; a third
    // is a state machine that wants splitting.
    reg [1:0] sub_r;
    always @(*) begin
        sub_r = 2'd0;
        case (st)
            S_DATA: begin
                case (wptr[1:0])
                    2'd0:    sub_r = 2'd1;
                    2'd1:    sub_r = 2'd2;
                    default: sub_r = 2'd3;
                endcase
            end
            default: begin
                sub_r = 2'd0;
            end
        endcase
    end

    // `casez` treats z in the LABEL as a don't-care, and `?` is the readable
    // spelling of z in that position. A priority decoder is what it is for, and
    // the arms are written in priority order with that stated.
    reg [1:0] pri_r;
    always @(*) begin
        // First match wins, so the order of these arms IS the priority.
        casez (p_gnt[1:0])
            2'b?1:   pri_r = 2'd0;
            2'b1?:   pri_r = 2'd1;
            default: pri_r = 2'd2;
        endcase
    end

    // `casex` treats x in the SELECTOR as a don't-care too, which means an X
    // from an uninitialised register silently matches an arm. It is banned in
    // this tree for that reason; the shape is shown so it is recognisable, in a
    // dead branch so nothing depends on it.
    // casex (p_gnt) 2'b1x: ... endcase   -- BANNED, use casez.

    // A `full_case` / `parallel_case` pragma is also banned: it tells synthesis
    // something the simulator does not believe, and the two then disagree.

    // ========================================================================
    // 10. if / else CHAINS
    // ========================================================================
    // F4. Every body is a block, including the one-statement ones, because
    // Verilog's dangling `else` is legal and silent and a one-line body is one
    // edit away from being two statements of which only the first is guarded.
    reg [2:0] cls_r;
    always @(*) begin
        if (st == S_IDLE) begin
            cls_r = 3'd0;
        end else if (in_transfer) begin
            cls_r = 3'd1;
        end else if (st == S_DONE) begin
            cls_r = 3'd2;
        end else begin
            cls_r = 3'd7;
        end
    end

    // `end else if` on ONE line, never `end` and `else if` on two. The chain is
    // one decision and it should read as one.

    // A condition that FITS stays on the `if` line, however many terms it has.
    reg armed_r;
    always @(*) begin
        if (go && in_transfer && !stall) begin
            armed_r = 1'b1;
        end else begin
            armed_r = 1'b0;
        end
    end

    // A condition too long for its line follows F6, and the `begin` goes with
    // the closing paren so the body's brace is not orphaned on its own line.
    reg fire_r;
    always @(*) begin
        if (
            go
            && (st == S_DATA)
            && !stall
            && (wptr != {WSEL_W{1'b0}})
            && (p_gnt[0] || p_gnt[PORTS-1])
        ) begin
            fire_r = 1'b1;
        end else begin
            fire_r = 1'b0;
        end
    end

    // ========================================================================
    // 11. LOOPS
    // ========================================================================
    // F4 again: the body is a block even when it is one statement. The
    // single-line `for (...) x = 0;` form is the worst case of all, because the
    // header and the body share a line and neither is obviously the other's.
    reg [PORTS-1:0] req_r;
    always @(*) begin
        for (i = 0; i < PORTS; i = i + 1) begin
            req_r[i] = p_gnt[i] && go;
        end
    end

    // A loop over a 2-D array nests, and each level takes its own counter.
    // Reusing one counter across nested loops is a bug the tools will not find.
    reg [7:0] tsum_r;
    always @(*) begin
        tsum_r = 8'd0;
        for (i = 0; i < 4; i = i + 1) begin
            for (j = 0; j < 4; j = j + 1) begin
                tsum_r = tsum_r + tile[i][j];
            end
        end
    end

    // A NAMED block plus `disable` is the early exit, and the name says what is
    // being left. `disable` on an unnamed block is not expressible, which is
    // one more reason to name them.
    reg [PSEL_W-1:0] first_r;
    always @(*) begin
        first_r = {PSEL_W{1'b0}};
        begin : scan_ports
            for (i = 0; i < PORTS; i = i + 1) begin
                if (p_gnt[i]) begin
                    first_r = i[PSEL_W-1:0];
                    disable scan_ports;
                end
            end
        end
    end

    // ========================================================================
    // 12. FUNCTIONS
    // ========================================================================
    // A function's return width is on the `function` line, its arguments are
    // one per line, its locals follow them, and its body is a begin/end block
    // even when it is one statement -- F4 does not exempt functions.
    //
    // `automatic` when the function can be called more than once in the same
    // expression: a static function shares its locals between calls, and two
    // calls in one `assign` then corrupt each other.
    function automatic [DATA_W-1:0] fmt_pick;
        input [DATA_W-1:0] a;
        input [DATA_W-1:0] b;
        input              take_a;
        begin
            fmt_pick = take_a ? a : b;
        end
    endfunction

    // A function with real logic in it: locals declared, then the body. The
    // return variable is assigned on every path, because a function that falls
    // through returns X and nothing warns.
    function automatic [2:0] fmt_region;
        input [ADDR_W-1:0] a;
        reg [2:0] r;
        begin
            if (a[ADDR_W-1]) begin
                r = 3'd5;
            end else begin
                case (a[ADDR_W-2 : ADDR_W-4])
                    3'd1:    r = 3'd1;
                    3'd2:    r = 3'd2;
                    default: r = 3'd7;
                endcase
            end
            fmt_region = r;
        end
    endfunction

    // A CONSTANT function -- Verilog-2001's way to compute a width before
    // `$clog2` existed. `$clog2` is in the language now and is what this tree
    // uses; this shape survives only where a tool rejects `$clog2` in a
    // particular position.
    function integer fmt_clog2;
        input integer v;
        integer t;
        integer n;
        begin
            n = 0;
            t = v - 1;
            while (t > 0) begin
                n = n + 1;
                t = t >> 1;
            end
            fmt_clog2 = n;
        end
    endfunction

    localparam integer CF_W = fmt_clog2(WORDS);

    // A CALL that fits stays on one line -- the argument list is not a reason to
    // wrap by itself.
    wire [DATA_W-1:0] picked = fmt_pick(hold_q, d_in, busy_q);

    // It breaks one argument per line only when it will not fit, and the call's
    // own parentheses then play F6's role.
    wire [DATA_W-1:0] picked_long = fmt_pick(
        fmt_pick(p0_q, p1_q, busy_q),
        fmt_pick(p2_q, hold_q, s_last),
        (st == S_DATA) && !stall
    );

    // ========================================================================
    // 13. TASKS
    // ========================================================================
    // Same layout as a function: arguments one per line, direction first, and a
    // begin/end body. A task is the right shape when there is more than one
    // output or when the body needs a statement a function may not contain.
    //
    // Tasks appear in this tree's BENCHES and almost never in its RTL, because
    // a task called from two always blocks is two copies of the logic and the
    // duplication is invisible at the call site.
    task automatic fmt_split;
        input  [DATA_W-1:0] word;
        output [15:0]       hi;
        output [15:0]       lo;
        begin
            hi = word[31:16];
            lo = word[15:0];
        end
    endtask

    // ========================================================================
    // 14. SEQUENTIAL always
    // ========================================================================
    // F10. `<=` throughout, and every register assigned in exactly one always
    // block. A register driven from two blocks is a race in simulation and a
    // multiple-driver error in synthesis, in that order.
    //
    // The reset arm comes first and lists every register the block owns. A
    // register missing from it is X out of reset, which propagates through a
    // comparison and disappears.
    always @(posedge clk) begin
        if (!resetn) begin
            st     <= S_IDLE;
            wptr   <= {WSEL_W{1'b0}};
            hold_q <= {DATA_W{1'b0}};
            busy_q <= 1'b0;
        end else begin
            st <= next_st;
            if (go) begin
                hold_q <= d_in;
                wptr   <= wptr + 1'b1;
            end
            busy_q <= in_transfer;
        end
    end

    // A block with NO reset is written without one, deliberately and with the
    // reason stated -- not by forgetting. This tree's own experience: Vivado
    // routes a reset-free datapath as a reset one anyway when a control set
    // makes it convenient, so the netlist is the only place to confirm it.
    // Datapath only: `p0_q` is overwritten every cycle a transfer is live and
    // is never read before it is written, so a reset here costs control sets
    // and buys nothing.
    always @(posedge clk) begin
        p0_q <= d_in;
        p1_q <= p0_q;
        p2_q <= p1_q;
    end

    // An asynchronous reset, for the one place it belongs: a reset
    // synchroniser. Everywhere else the reset is synchronous, because an async
    // reset needs a recovery/removal constraint that nothing else here has.
    reg [1:0] rst_sync_q;
    always @(posedge clk or negedge resetn) begin
        if (!resetn) begin
            rst_sync_q <= 2'b00;
        end else begin
            rst_sync_q <= {rst_sync_q[0], 1'b1};
        end
    end

    // A write to an unpacked array. The address is registered by the time it
    // gets here, because a block RAM's address pin is where an adder must not
    // end.
    always @(posedge clk) begin
        if (go) begin
            mem[wptr] <= d_in;
        end
    end

    // ========================================================================
    // 15. generate
    // ========================================================================
    // F7. `generate` and `endgenerate` do NOT add an indent level -- the region
    // is a compile-time construct and indenting it a second time pushes real
    // code two levels right for nothing. What they DO require is a name on
    // every block, because an unnamed one gets a tool-assigned label and a
    // hierarchical reference into it is then unstable across tool versions.
    // A bench probe is exactly such a reference, and a rename inside one has
    // broken benches here before.
    //
    // `genvar` is declared immediately above the loop that uses it.
    genvar gi;
    generate
    for (gi = 0; gi < PORTS; gi = gi + 1) begin : g_port
        // A wire declared inside a generate block is per-instance, and its
        // hierarchical name is `g_port[0].base_g` -- which is the other half of
        // why the block is named.
        wire [ADDR_W-1:0] base_g = LIT_ADDR + (gi * WORDS);
        assign p_addr[gi*ADDR_W +: ADDR_W] = base_g;
        assign p_req[gi]                   = req_r[gi];
    end
    endgenerate

    // An if-generate names BOTH arms, and the names say what each selects --
    // `g_has_l2` / `g_no_l2`, not `g_a` / `g_b`.
    wire [DATA_W-1:0] l2_data;
    generate
    if (HAS_L2 != 0) begin : g_has_l2
        reg [DATA_W-1:0] l2_q;
        always @(posedge clk) begin
            if (!resetn) begin
                l2_q <= {DATA_W{1'b0}};
            end else begin
                l2_q <= d_in;
            end
        end
        assign l2_data = l2_q;
    end else begin : g_no_l2
        // The tie-off arm exists so the port has a driver in every
        // configuration. An undriven input is Z, not 0, and Z through a mux is
        // X -- which is how a config path went X here and a mover silently took
        // no descriptor.
        assign l2_data = {DATA_W{1'b0}};
    end
    endgenerate

    // A case-generate follows F5 for its items and F7 for its region: the items
    // indent one level inside `case`, the region itself does not indent.
    wire [1:0] mode_tag;
    generate
    case (PORTS)
        1: begin : g_one_port
            assign mode_tag = 2'd0;
        end
        2: begin : g_two_ports
            assign mode_tag = 2'd1;
        end
        default: begin : g_many_ports
            assign mode_tag = 2'd2;
        end
    endcase
    endgenerate

    // A NESTED generate loop indents its inner loop but still not the region,
    // and every level is named -- the reference is `g_row[1].g_col[2].t_g`.
    genvar gr, gc;
    generate
    for (gr = 0; gr < 4; gr = gr + 1) begin : g_row
        for (gc = 0; gc < 4; gc = gc + 1) begin : g_col
            wire [7:0] t_g = tile[gr][gc];
            // A generate block may hold an always block, and this is the shape
            // that makes a per-lane register array without an unpacked port.
            always @(posedge clk) begin
                if (!resetn) begin
                    tile[gr][gc] <= 8'd0;
                end else if (go) begin
                    tile[gr][gc] <= t_g + 8'd1;
                end
            end
        end
    end
    endgenerate

    // ========================================================================
    // 16. INSTANTIATION
    // ========================================================================
    // F8. Named ports, one per line, `.name (expr)` with the parens aligned.
    // POSITIONAL connection is banned outright: it is correct until someone
    // reorders the child's ports, and then it is silently wrong.
    //
    // Every port is written, including the unused ones, so the reader can tell
    // "deliberately unused" from "forgotten" -- and so the linter can. An
    // omitted INPUT is the dangerous half: it is Z, not 0.
    wire [DATA_W-1:0] leaf_data;
    wire              leaf_valid;

    fmt_leaf #(
        .W    (DATA_W),
        .INIT (INIT_WORD[DATA_W-1:0])
    ) u_leaf (
        .clk    (clk),
        .resetn (resetn),
        .en     (go),
        .d      (d_in),
        .q      (leaf_data),
        .q_v    (leaf_valid)
    );

    // A module with no parameter override drops the `#()` entirely rather than
    // writing an empty one.
    wire [DATA_W-1:0] leaf2_data;
    wire              leaf2_valid;

    fmt_leaf u_leaf_default (
        .clk    (clk),
        .resetn (resetn),
        .en     (busy_q),
        .d      (hold_q),
        .q      (leaf2_data),
        .q_v    (leaf2_valid)
    );

    // An expression on a port stays inline while it FITS -- a port map is not a
    // place where every expression has to become a wire.
    wire [DATA_W-1:0] leaf3_data;
    wire              leaf3_valid;

    fmt_leaf u_leaf_expr (
        .clk    (clk),
        .resetn (resetn),
        .en     (go && !stall && (st == S_DATA)),
        .d      (picked),
        .q      (leaf3_data),
        .q_v    (leaf3_valid)
    );

    // A COMPLEX one wraps by F6 inside the port's own parentheses. If it needs
    // more than that it wants a named wire above the instantiation instead --
    // logic buried in a port map is logic nobody finds, and it cannot be probed
    // in a waveform because it has no name to probe.
    wire [DATA_W-1:0] leaf4_data;
    wire              leaf4_valid;

    fmt_leaf u_leaf_expr_long (
        .clk    (clk),
        .resetn (resetn),
        .en     (
            go
            && !stall
            && (st == S_DATA)
            && (wptr != {WSEL_W{1'b0}})
            && (p_gnt[0] || p_gnt[PORTS-1])
        ),
        .d      (picked),
        .q      (leaf4_data),
        .q_v    (leaf4_valid)
    );

    // An ARRAY OF INSTANCES is Verilog-2001 and it is the compact form of a
    // for-generate. This tree prefers the generate: the array form gives the
    // instances tool-assigned names, which is the same problem F7 exists to
    // solve, and it silently broadcasts a port that is narrower than the array.
    // fmt_leaf u_arr [PORTS-1:0] (...);   -- avoid, use a named generate.

    // `defparam` is banned. It overrides a parameter from OUTSIDE the
    // instantiation, so the value at the instance is not visible at the
    // instance, and it is deprecated in every version after 2001.

    // ========================================================================
    // 17. OUTPUT DRIVERS
    // ========================================================================
    // The module's outputs are driven in one place at the bottom, so the
    // boundary is readable without a search. One `assign` per port, aligned.
    assign m_valid = busy_q && !stall;
    assign m_data  = out_d;
    assign m_last  = busy_q && s_last;
    assign busy    = busy_q;

    // ========================================================================
    // 18. SIMULATION-ONLY CODE
    // ========================================================================
    // F12 puts the guard at column 0. Everything that cannot synthesise lives
    // inside one, and the guard names what it excludes rather than what it
    // includes, so the synthesised build is the default.
`ifndef SYNTHESIS
    // An `initial` block that writes a register an always block also drives is
    // BANNED, even here: `=` and `<=` to one variable is a race, which is what
    // F10's second half rules out. The bench module below is where `initial`
    // belongs. On silicon it initialises nothing anyway -- an FPGA's power-on
    // state comes from the bitstream and the reset is still required.
    //
    // A concurrent CHECK, which is what this tree uses instead of a
    // SystemVerilog assertion. It reports and it counts, and a bench that
    // cannot fail has reported a pass it did not earn.
    always @(posedge clk) begin
        if (resetn && (st === 3'bxxx)) begin
            $display("FAIL %0t state went X", $time);
            $finish;
        end
    end

    // `===` and `!==` are the simulation-only comparisons and they are the RIGHT
    // ones in a check: `==` against an X operand is X, which is falsy, so a
    // check written with `==` passes silently on exactly the bug it was written
    // to catch.
    always @(posedge clk) begin
        if (busy_q === 1'bx) begin
            $display("FAIL %0t busy_q is X", $time);
        end
    end

    // Display formatting: one `$display` per line, the format string first, and
    // `%0d` / `%0h` rather than `%d` / `%h` so the column padding does not move
    // when a value grows.
    always @(posedge clk) begin
        if (go) begin
            $display("[fmt] %0t wptr=%0d data=%0h", $time, wptr, d_in);
        end
    end
`endif

endmodule


// ============================================================================
// 19. A SECOND MODULE IN THE SAME FILE
// ============================================================================
// Two blank lines between modules, and each module gets its own banner. One
// module per file is the rule this tree follows; a second one belongs here only
// when it exists solely to serve the first, as this leaf does.
module fmt_leaf #(
    parameter integer W    = 32,
    parameter [31:0]  INIT = 32'h0000_0000
) (
    input  wire         clk,
    input  wire         resetn,
    input  wire         en,
    input  wire [W-1:0] d,
    output wire [W-1:0] q,
    output wire         q_v
);

    reg [W-1:0] q_r;
    reg         v_r;

    always @(posedge clk) begin
        if (!resetn) begin
            q_r <= INIT[W-1:0];
            v_r <= 1'b0;
        end else if (en) begin
            q_r <= d;
            v_r <= 1'b1;
        end else begin
            v_r <= 1'b0;
        end
    end

    assign q   = q_r;
    assign q_v = v_r;

endmodule


// ============================================================================
// 20. BENCH-ONLY SHAPES
// ============================================================================
// The constructs a testbench has and RTL does not. They follow the same rules;
// the differences are which forms are permitted at all.
`ifndef SYNTHESIS
module fmt_bench_shapes;

    // A bench declares its clock and reset itself, and the period is a named
    // parameter so a re-time is one edit.
    localparam integer TCK = 4;

    reg         clk = 1'b0;
    reg         resetn = 1'b0;
    reg  [31:0] data = 32'd0;
    reg         valid = 1'b0;
    wire [31:0] q;
    wire        q_v;

    // `real` and `time` exist and are simulation only. `real` in particular is
    // not synthesisable at all, which is why it appears here and nowhere above.
    real    elapsed_ns;
    time    t_start;
    integer errors = 0;
    integer checks = 0;

    // An event is Verilog-2001's lightweight synchronisation. This tree uses
    // plain flags instead -- an event has no state, so a wait that arrives one
    // delta late waits forever.
    event tick_done;

    // A clock generator: one `always` with a delay, and the half period written
    // as arithmetic on the named parameter rather than as a second constant.
    always begin
        #(TCK / 2) clk = ~clk;
    end

    // A WATCHDOG, and it is not optional: a bench that can expire quietly has
    // reported a pass it did not earn. The bound is in the same units as the
    // clock, and note that xsim's delays are 64-bit -- a watchdog that did not
    // fire is never a 32-bit picosecond wrap.
    initial begin
        #200000;
        $display("FAIL watchdog -- bench did not finish");
        $fatal(1);
    end

    fmt_leaf #(
        .W    (32),
        .INIT (32'hFFFF_FFFF)
    ) dut (
        .clk    (clk),
        .resetn (resetn),
        .en     (valid),
        .d      (data),
        .q      (q),
        .q_v    (q_v)
    );

    // A check TASK, so every comparison reports the same way and increments the
    // same two counters. A bench with hand-written comparisons has as many
    // report formats as it has checks.
    task automatic expect_eq;
        input [255:0] name;
        input [31:0]  got;
        input [31:0]  want;
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                $display("FAIL %0s: got %08h want %08h", name, got, want);
            end
        end
    endtask

    // A stimulus task takes its arguments one per line like any other.
    task automatic drive;
        input [31:0]  word;
        input integer cycles;
        begin
            data  = word;
            valid = 1'b1;
            repeat (cycles) begin
                @(posedge clk);
            end
            valid = 1'b0;
        end
    endtask

    // The main sequence. Timing controls are statements and take a line each:
    // `@(posedge clk)` and `#N` buried at the end of an assignment hide when
    // the assignment happens.
    initial begin
        t_start = $time;
        resetn  = 1'b0;
        repeat (4) begin
            @(posedge clk);
        end
        resetn = 1'b1;

        drive(32'hA5A5_A5A5, 1);
        @(posedge clk);
        expect_eq("first word", q, 32'hA5A5_A5A5);

        // A `while` loop in a bench takes begin/end like every other body. Wait
        // for the condition the DESIGN will change, never for one only more
        // stimulus can: `while (!q_v)` here would spin until the watchdog,
        // because `drive` has already dropped `valid` and nothing else will
        // raise it. This waits for the flag to fall, which the design does.
        while (q_v) begin
            @(posedge clk);
        end

        // `wait` is level-sensitive and returns immediately if the condition is
        // already true, which is usually what a bench wants and occasionally a
        // trap: a wait on a one-cycle pulse that has already passed hangs.
        wait (resetn === 1'b1);

        // `fork`/`join` runs its arms concurrently. `join_any` and `join_none`
        // are SystemVerilog; Verilog-2001 has only `join`, which waits for all
        // of them.
        fork
            begin : arm_drive
                drive(32'h1234_5678, 2);
            end
            begin : arm_watch
                @(posedge q_v);
                elapsed_ns = ($time - t_start) * 1.0;
            end
        join

        expect_eq("second word", q, 32'h1234_5678);

        // `force` / `release` overrides a net from outside its driver. It is a
        // debugging instrument, never part of a passing bench: a forced net
        // proves the bench can be made to pass, not that the design works.
        force dut.q_r = 32'hDEAD_BEEF;
        @(posedge clk);
        release dut.q_r;

        // A hierarchical reference reaches into the DUT, and it is exactly why
        // F7 requires named generate blocks: `dut.g_port[0].u_eng` is a
        // contract, and a tool-assigned label is not.
        $display("[bench] internal v_r = %0b", dut.v_r);

        // Every bench ends with one line the harness can grep, and the pass
        // condition is `errors == 0` and not the absence of a FAIL print.
        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

endmodule
`endif


// ============================================================================
// 21. FORMS THIS TREE DOES NOT USE
// ============================================================================
// Shown once so they are recognisable, and marked so nobody adds more.
//
// `specify` blocks carry timing arcs for a cell library. Nothing here is a
// library cell, so the only correct number of these is zero. The shape:
//
//     specify
//         (a => y) = (1.0, 1.0);
//     endspecify
//
// A UDP is a truth table as a module. It has no place in synthesised RTL and
// the simulator's own primitives cover every case a bench needs. The shape:
//
//     primitive fmt_udp_mux (q, s, a, b);
//         output q;
//         input  s, a, b;
//         table
//             0 1 ? : 1;
//             1 ? 1 : 1;
//         endtable
//     endprimitive
//
// `wand` / `wor` / `tri` / `trireg` and strength specifications resolve
// multiple drivers instead of reporting them. A resolved net hides exactly the
// conflict that `default_nettype none` plus one driver per net is there to
// catch.
//
// `deassign` and procedural continuous `assign` inside a block are simulation
// artefacts that shadow a net's real driver. `force` / `release` above is the
// debugging instrument; these are the same hazard without the intent.

`default_nettype wire
