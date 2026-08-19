// The example project's RTL half: y[0:n] = a*x[0:n] + y[0:n] over float32,
// one instruction. The software half is driver/examples/saxpy (CU_TYPE 'SX';
// the payload layout here is field-for-field sw/isa.py). Built from
// templates/cu on noc_cu_base; memory access is plain reads and one burst
// write per memory-protocol.md s3.1/s4, framed by type and tag.
//
// The datapath is an example, not an FPU: exact for WHOLE-VALUED float32
// with |v| < 2^23 (f32 -> int, integer MAC, int -> f32), which is bit-exact
// against the software model on that domain. A real unit swaps f2i/mac/i2f
// for an FMA and keeps every other line.
`default_nettype none

module saxpy_cu #(
    parameter FLIT_WIDTH = 288,
    parameter POS_WIDTH  = 4,
    parameter POS_X      = 2,
    parameter POS_Y      = 2,
    parameter MEM_X      = 0,           // the memory agent port this unit uses
    parameter MEM_Y      = 1,
    parameter CU_TYPE    = 16'h5358,    // "SX", matching sw/unit.py
    parameter CU_VERSION = 8'h01,
    parameter INST_DEPTH = 32
)(
    input  wire                   clk,
    input  wire                   resetn,
    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,
    output wire                   busy
);
    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2, T_MEM_WR_ACK = 4'h3,
                     T_MEM_WR_DATA = 4'h4;

    localparam [7:0] OP_SAXPY = 8'h01;
    // 64 float32 = 8 beats, the agent's write-burst ceiling (WBURST).
    localparam N_MAX = 64;
    localparam [31:0] FAULT_BAD_OP = 32'hBAD0_0001,
                      FAULT_TOO_BIG = 32'hBAD0_0002;

    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit;
    wire                  inst_valid, recv_valid, send_ready;
    reg                   inst_ready, exec_done, exec_fault;
    reg  [31:0]           exec_result;
    wire [15:0]           inst_space;

    wire [7:0]  i_op = inst_flit[255 -: 8];
    wire [23:0] i_n  = inst_flit[247 -: 24];

    function signed [31:0] f2i(input [31:0] b);
        reg [7:0]  e;
        reg [31:0] mag;
        begin
            e = b[30:23];
            if (e == 8'd0)        mag = 32'd0;
            else if (e >= 8'd150) mag = {8'd0, 1'b1, b[22:0]} << (e - 8'd150);
            else                  mag = {8'd0, 1'b1, b[22:0]} >> (8'd150 - e);
            f2i = b[31] ? -$signed(mag) : $signed(mag);
        end
    endfunction

    function [31:0] i2f(input signed [31:0] v);
        reg [31:0] mag, sh;
        reg [7:0]  e;
        integer    i, p;
        begin
            mag = v[31] ? -v : v;
            p = 0;
            for (i = 0; i < 24; i = i + 1) if (mag[i]) p = i;
            e  = 8'd127 + p;
            sh = mag << (23 - p);
            i2f = (mag == 32'd0) ? 32'd0 : {v[31], e, sh[22:0]};
        end
    endfunction

    localparam [2:0] S_IDLE = 3'd0, S_RQX = 3'd1, S_RQY = 3'd2, S_WAIT = 3'd3,
                     S_COMP = 3'd4, S_WREQ = 3'd5, S_WDATA = 3'd6, S_DONE = 3'd7;
    localparam [7:0] TAG_X = 8'h10, TAG_Y = 8'h20, TAG_W = 8'h30;

    reg [2:0]   st;
    reg [23:0]  n_q;
    reg [31:0]  a_q;
    reg [39:0]  x_addr, y_addr;      // the agent's 40-bit address field
    reg [3:0]   beats;               // 256-bit lines per operand, 1..8
    reg [3:0]   xw, yw, wb;
    reg [6:0]   ei;
    reg         fault_q;
    reg [31:0]  fault_code;
    reg [2047:0] xbuf, ybuf;         // flat: variable part-select needs no array
    reg [31:0]  elements;            // cumulative, CU_DBG low half (sw/unit.py)

    reg [FLIT_WIDTH-1:0] send_flit;
    reg                  send_valid;

    wire [3:0] r_ty  = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire [7:0] r_txn = recv_flit[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];
    wire       recv_ready_w = 1'b1;  // always drain; a held flit wedges the queue

    always @(posedge clk) begin
        inst_ready <= 1'b0;
        exec_done  <= 1'b0;
        exec_fault <= 1'b0;
        if (!resetn) begin
            st <= S_IDLE;
            send_valid <= 1'b0;
            elements <= 32'd0;
        end else begin
            if (send_valid && send_ready) send_valid <= 1'b0;

            // responses: demuxed by type and tag, consumed whatever the state
            if (recv_valid) begin
                if (r_ty == T_MEM_RD_RESP && r_txn == TAG_X) begin
                    xbuf[xw*256 +: 256] <= recv_flit[255:0];
                    xw <= xw + 4'd1;
                end else if (r_ty == T_MEM_RD_RESP && r_txn == TAG_Y) begin
                    ybuf[yw*256 +: 256] <= recv_flit[255:0];
                    yw <= yw + 4'd1;
                end else if (r_ty != T_MEM_WR_ACK) begin
                    // synthesis translate_off
                    $display("%0t saxpy_cu: dropping flit type %0h", $time, r_ty);
                    // synthesis translate_on
                end
            end

            case (st)
            S_IDLE: begin
                // Accept, THEN retire on a later cycle (noc_cu_base:221).
                if (inst_valid && !inst_ready) begin
                    inst_ready <= 1'b1;
                    n_q     <= i_n;
                    a_q     <= inst_flit[223 -: 32];
                    x_addr  <= inst_flit[191 -: 64];   // truncates to 40 bits
                    y_addr  <= inst_flit[127 -: 64];
                    beats   <= i_n[6:3] + {3'd0, |i_n[2:0]};
                    xw <= 4'd0; yw <= 4'd0; wb <= 4'd0; ei <= 7'd0;
                    fault_q <= 1'b0;
                    if (i_op != OP_SAXPY) begin
                        fault_q <= 1'b1; fault_code <= FAULT_BAD_OP;
                        st <= S_DONE;
                    end else if (i_n > N_MAX) begin
                        fault_q <= 1'b1; fault_code <= FAULT_TOO_BIG;
                        st <= S_DONE;
                    end else if (i_n == 24'd0) st <= S_DONE;
                    else st <= S_RQX;
                end
            end
            // Plain read (STREAM=0): one RD_RESP per beat, txn echoed on each.
            S_RQX: if (!send_valid || send_ready) begin
                send_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                               POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                               T_MEM_RD_REQ, TAG_X, 1'b1, 3'b000,
                               x_addr, {4'd0, beats} - 8'd1, 8'd0, 200'd0 };
                send_valid <= 1'b1;
                st <= S_RQY;
            end
            S_RQY: if (!send_valid || send_ready) begin
                send_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                               POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                               T_MEM_RD_REQ, TAG_Y, 1'b1, 3'b000,
                               y_addr, {4'd0, beats} - 8'd1, 8'd0, 200'd0 };
                send_valid <= 1'b1;
                st <= S_WAIT;
            end
            S_WAIT: if (xw == beats && yw == beats) st <= S_COMP;
            // One element per cycle. The tail of a partial last line is left
            // as read, so the write-back is a read-modify-write of that line.
            S_COMP: begin
                ybuf[ei*32 +: 32] <= i2f(f2i(a_q) * f2i(xbuf[ei*32 +: 32])
                                         + f2i(ybuf[ei*32 +: 32]));
                elements <= elements + 32'd1;
                if ({17'd0, ei} == n_q - 24'd1) st <= S_WREQ;
                else ei <= ei + 7'd1;
            end
            // One descriptor, len+1 data flits, last on the final one. The
            // ack is dropped above: nobody sequences on it (s5).
            S_WREQ: if (!send_valid || send_ready) begin
                send_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                               POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                               T_MEM_WR_REQ, TAG_W, 1'b0, 3'b000,
                               y_addr, {4'd0, beats} - 8'd1, 8'd0, 200'd0 };
                send_valid <= 1'b1;
                st <= S_WDATA;
            end
            S_WDATA: if (!send_valid || send_ready) begin
                send_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                               POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                               T_MEM_WR_DATA, TAG_W, (wb == beats - 4'd1),
                               3'b000, ybuf[wb*256 +: 256] };
                send_valid <= 1'b1;
                if (wb == beats - 4'd1) st <= S_DONE;
                else wb <= wb + 4'd1;
            end
            S_DONE: begin
                exec_done   <= 1'b1;
                exec_fault  <= fault_q;
                exec_result <= fault_q ? fault_code : {8'd0, n_q};
                st <= S_IDLE;
            end
            default: st <= S_IDLE;
            endcase
        end
    end

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y),
        .CU_TYPE(CU_TYPE), .CU_VERSION(CU_VERSION), .N_BUFFERS(1),
        .INST_DEPTH(INST_DEPTH), .MEM_TYPE("distributed")
    ) u_base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result),
        .exec_fault(exec_fault),
        .dbg_ctr({32'd0, elements}),   // sw/unit.py decode_dbg: elements, low half
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid),
        .recv_ready(recv_ready_w),
        .inst_space(inst_space), .busy(busy)
    );

endmodule

`default_nettype wire
