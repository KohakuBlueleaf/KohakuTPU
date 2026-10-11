// KohakuTPU's occupants of the memory agent's transform slot.
//
//   id 0  bypass -- four beats pass through as words
//   id 1  FP16 -> MXFP7 block quantiser (mx_quant), 2048 bits in, 1024 out
//   id 2  GT4 -- four words as a 4 x 4 array of 64-bit granules, transposed:
//         out word i, granule w = in word w, granule i (v2_gt4's function)
//
// The framework instantiates THIS module by name and never names a transform.
// A project with different arithmetic writes its own and changes nothing else;
// one with none uses the identity bank in src/templates/transform/.
//
// `mode` is opaque to the framework. Here mode[0] is the A/B operand packing
// select -- what the protocol used to call BLAYOUT.
//
// GEOMETRY BY ID (`geo_*`): the beats one entry takes and how many entries the
// occupant holds at once. The requester sizes its entries by the first and
// keeps at most the second started and not `done`.

`default_nettype none

// The framework never names occupant parameters, so the quantiser's buffer
// count is a define: `-d KH_XF_Q_DBUF=0` builds it single-buffered.
`ifndef KH_XF_Q_DBUF
`define KH_XF_Q_DBUF 1
`endif

module xform_bank #(
    parameter integer DATA_W    = 256,
    parameter integer SLOTS     = 1,
    parameter integer ID_W      = 1,
    parameter integer MODE_W    = 1,
    parameter integer IN_BITS   = 2048,
    parameter integer OUT_WORDS = 4,
    parameter integer Q_DBUF    = `KH_XF_Q_DBUF  // the quantiser's second source buffer
)(
    input  wire                 clk,
    input  wire                 rst,
    input  wire                 start,
    input  wire [ID_W-1:0]      id,
    input  wire [MODE_W-1:0]    mode,
    input  wire [DATA_W-1:0]    beat,
    input  wire                 beat_valid,
    output wire                 need_beat,
    output wire                 done,
    output wire [DATA_W-1:0]    word0, word1, word2, word3,

    input  wire [ID_W-1:0]      geo_id,
    output wire [3:0]           geo_in_beats,
    output wire [2:0]           geo_depth,

    // ---- the occupant register space, reached by the control processor ----
    input  wire                 cfg_en,       // write strobe
    input  wire [ID_W-1:0]      cfg_id,       // which occupant
    input  wire [7:0]           cfg_addr,     // byte offset, 4-byte registers
    input  wire [31:0]          cfg_data,
    output reg  [31:0]          cfg_rdata,    // combinational read of cfg_addr
    output wire [3:0]           fault
);
    localparam [ID_W-1:0] ID_BYPASS = 0;
    localparam [ID_W-1:0] ID_QUANT  = 1;
    localparam [ID_W-1:0] ID_GT4    = 2;

    // SIZED. An integer expression contributes 32 bits inside a concatenation
    // whatever it holds, so the geometry word below would shift its own fields.
    localparam [15:0] Q_IN_BITS = IN_BITS;
    localparam [7:0]  Q_OUT_W   = OUT_WORDS;
    localparam [15:0] P_IN_BITS = 4 * DATA_W;
    localparam [7:0]  P_OUT_W   = 8'd4;
    localparam [3:0]  Q_BEATS   = IN_BITS / DATA_W;
    localparam [2:0]  Q_DEPTH   = (Q_DBUF != 0) ? 3'd2 : 3'd1;

    wire sel_q = (id == ID_QUANT);
    wire sel_g = (id == ID_GT4);
    wire sel_p = !sel_q && !sel_g;          // bypass, and the answer to a bad id

    assign geo_in_beats = (geo_id == ID_QUANT) ? Q_BEATS : 4'd4;
    assign geo_depth    = (geo_id == ID_QUANT) ? Q_DEPTH : 3'd2;

    wire             q_done;
    wire [DATA_W-1:0] q_w0, q_w1, q_w2, q_w3;

    mx_quant #(.DBUF(Q_DBUF)) u_quant (
        .clk(clk), .rst(rst),
        .start(start && sel_q),
        .b_layout(mode[0]),
        .beat(beat),
        .beat_valid(beat_valid && sel_q),
        .need_beat(), .done(q_done),
        .word0(q_w0), .word1(q_w1), .word2(q_w2), .word3(q_w3)
    );

    // ids 0 and 2: four beats in, four words out, done a cycle after the last.
    // The first three beats are held; the words are registered from them and
    // the fourth, so the next entry's beats may follow at once.
    reg [1:0]        p_cnt;
    reg [DATA_W-1:0] p_b0, p_b1, p_b2;
    reg [DATA_W-1:0] p_w0, p_w1, p_w2, p_w3;
    reg              p_done;
    wire [DATA_W-1:0] in_w [0:3];
    assign in_w[0] = p_b0;
    assign in_w[1] = p_b1;
    assign in_w[2] = p_b2;
    assign in_w[3] = beat;
    localparam integer G = DATA_W / 4;
    reg [DATA_W-1:0] g_o [0:3];
    integer gi, gw;
    always @(*) begin
        for (gi = 0; gi < 4; gi = gi + 1) begin
            for (gw = 0; gw < 4; gw = gw + 1) begin
                g_o[gi][gw*G +: G] = in_w[gw][gi*G +: G];
            end
        end
    end

    // A beat with `start` is the entry's first.
    wire [1:0] p_bc = start ? 2'd0 : p_cnt;
    always @(posedge clk) begin
        p_done <= 1'b0;
        if (rst) begin
            p_cnt <= 2'd0;
        end else if (start && !sel_q && !beat_valid) begin
            p_cnt <= 2'd0;
        end else if (beat_valid && !sel_q) begin
            case (p_bc)
                2'd0: p_b0 <= beat;
                2'd1: p_b1 <= beat;
                2'd2: p_b2 <= beat;
                default: begin
                    p_w0 <= sel_g ? g_o[0] : p_b0;
                    p_w1 <= sel_g ? g_o[1] : p_b1;
                    p_w2 <= sel_g ? g_o[2] : p_b2;
                    p_w3 <= sel_g ? g_o[3] : beat;
                    p_done <= 1'b1;
                end
            endcase
            p_cnt <= p_bc + 2'd1;
        end
    end

    // ---- status ----------------------------------------------------------
    // THE ONE FAULT A BANK CAN DETECT ITSELF: an id naming no occupant. The
    // demux above answers such an id with the bypass path, so without this the
    // move completes, reports success, and delivers an unconverted operand.
    reg [3:0] flt;
    always @(posedge clk) begin
        if (rst) begin
            flt <= 4'd0;
        end
        else if (cfg_en && (cfg_addr[7:2] == 6'd0)) begin
            flt <= 4'd0;
        end
        else if (start && sel_p && (id != ID_BYPASS)) begin
            flt[0] <= 1'b1;
        end
    end
    assign fault = flt;

    // Geometry per id, so a driver can discover what a slot holds rather than
    // being told. An id that names no occupant reads zero.
    always @(*) begin
        case (cfg_addr[7:2])
            6'd0:    cfg_rdata = {28'd0, flt};
            6'd1:    cfg_rdata = (cfg_id == ID_QUANT)
                               ? {8'd0, Q_OUT_W, Q_IN_BITS}
                               : ((cfg_id == ID_BYPASS) || (cfg_id == ID_GT4))
                                 ? {8'd0, P_OUT_W, P_IN_BITS}
                                 : 32'd0;
            default: cfg_rdata = 32'd0;
        endcase
    end

    assign need_beat = 1'b1;
    assign done  = sel_q ? q_done : p_done;
    assign word0 = sel_q ? q_w0 : p_w0;
    assign word1 = sel_q ? q_w1 : p_w1;
    assign word2 = sel_q ? q_w2 : p_w2;
    assign word3 = sel_q ? q_w3 : p_w3;
endmodule

`default_nettype wire
