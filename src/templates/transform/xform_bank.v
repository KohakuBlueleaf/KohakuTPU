// THE IDENTITY BANK: the transform slot with nothing in it.
//
// `mag_xform` instantiates `xform_bank` by name, and that is the ONE module name
// the framework fixes. A project supplying a transform writes its own file with
// this module name; a project with none compiles THIS one instead, and the
// framework then elaborates with no project sources at all.
//
// Every id is bypass: four beats in, four words out, `done` on the last, so a
// requester gets the fixed output shape of spec/transform-slot.md rule 1 rather
// than a dangling `done`. Nothing here computes anything, so the read path is a
// register stage and the slot's empty state costs almost nothing -- the same
// property that makes `noc_l2_adapter`'s PASS=1 a slot people leave in.

`default_nettype none

module xform_bank #(
    parameter integer DATA_W    = 256,
    parameter integer SLOTS     = 1,
    parameter integer ID_W      = 1,
    parameter integer MODE_W    = 1,
    parameter integer IN_BITS   = 1024,
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

    input  wire                 cfg_en,
    input  wire [ID_W-1:0]      cfg_id,
    input  wire [7:0]           cfg_addr,
    input  wire [31:0]          cfg_data,
    output reg  [31:0]          cfg_rdata,
    output wire [3:0]           fault
);
    // SIZED: an integer expression contributes 32 bits inside a concatenation.
    localparam [15:0] P_IN_BITS = 4 * DATA_W;
    localparam [7:0]  P_OUT_W   = 8'd4;

    reg [1:0]        p_cnt;
    reg [DATA_W-1:0] p_w0, p_w1, p_w2, p_w3;
    reg              p_done;

    always @(posedge clk) begin
        p_done <= 1'b0;
        if (rst) begin
            p_cnt <= 2'd0;
        end else if (start) begin
            p_cnt <= 2'd0;
        end else if (beat_valid) begin
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

    // Every id is bypass here, so no id can be wrong and the fault is constant.
    always @(*) begin
        case (cfg_addr[7:2])
            6'd0:    cfg_rdata = 32'd0;
            6'd1:    cfg_rdata = {8'd0, P_OUT_W, P_IN_BITS};
            default: cfg_rdata = 32'd0;
        endcase
    end

    assign fault = 4'd0;
    assign need_beat = 1'b1;
    assign done  = p_done;
    assign word0 = p_w0;
    assign word1 = p_w1;
    assign word2 = p_w2;
    assign word3 = p_w3;
endmodule

`default_nettype wire
