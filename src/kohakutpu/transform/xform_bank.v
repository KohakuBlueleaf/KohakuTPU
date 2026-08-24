// KohakuTPU's occupants of the memory agent's transform slot.
//
//   id 0  bypass -- beats pass through as words
//   id 1  FP16 -> MXFP7 block quantiser (mx_quant), 2048 bits in, 1024 out
//
// The framework instantiates THIS module by name and never names a transform.
// A project with different arithmetic writes its own and changes nothing else;
// one with none uses the identity bank in src/templates/transform/.
//
// `mode` is opaque to the framework. Here mode[0] is the A/B operand packing
// select -- what the protocol used to call BLAYOUT.

`default_nettype none

module xform_bank #(
    parameter integer DATA_W    = 256,
    parameter integer SLOTS     = 1,
    parameter integer ID_W      = 1,
    parameter integer MODE_W    = 1,
    parameter integer IN_BITS   = 2048,
    parameter integer OUT_WORDS = 4
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

    // SIZED. An integer expression contributes 32 bits inside a concatenation
    // whatever it holds, so the geometry word below would shift its own fields.
    localparam [15:0] Q_IN_BITS = IN_BITS;
    localparam [7:0]  Q_OUT_W   = OUT_WORDS;
    localparam [15:0] P_IN_BITS = 4 * DATA_W;
    localparam [7:0]  P_OUT_W   = 8'd4;

    wire sel_q = (id == ID_QUANT);
    wire sel_p = (id == ID_BYPASS);

    wire             q_done;
    wire [DATA_W-1:0] q_w0, q_w1, q_w2, q_w3;

    mx_quant u_quant (
        .clk(clk), .rst(rst),
        .start(start && sel_q),
        .b_layout(mode[0]),
        .beat(beat),
        .beat_valid(beat_valid && sel_q),
        .need_beat(), .done(q_done),
        .word0(q_w0), .word1(q_w1), .word2(q_w2), .word3(q_w3)
    );

    // id 0: four beats in, four words out, done on the last. Present so a
    // requester that names bypass gets the fixed output shape rule 1 of
    // spec/transform-slot.md demands, rather than a dangling `done`.
    reg [1:0]        p_cnt;
    reg [DATA_W-1:0] p_w0, p_w1, p_w2, p_w3;
    reg              p_done;
    always @(posedge clk) begin
        p_done <= 1'b0;
        if (rst) begin
            p_cnt <= 2'd0;
        end else if (start && !sel_q) begin
            p_cnt <= 2'd0;
        end else if (beat_valid && !sel_q) begin
            case (p_cnt)
                2'd0: p_w0 <= beat;
                2'd1: p_w1 <= beat;
                2'd2: p_w2 <= beat;
                default: p_w3 <= beat;
            endcase
            p_cnt <= p_cnt + 2'd1;
            if (p_cnt == 2'd3) begin
                p_done <= 1'b1;
            end
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
        else if (start && !sel_q && !sel_p) begin
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
                               : (cfg_id == ID_BYPASS)
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
