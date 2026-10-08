// noc_cu_base ACK_FENCE: a writing instruction's CU_SIGNAL leaves only after
// its MEM_WR_ACK, held here HOLD cycles. -d FENCE=0 builds the unfenced endpoint.

`timescale 1ns/1ps
`default_nettype none

`ifndef FENCE
`define FENCE 1
`endif

module cu_ack_fence_tb;
    localparam integer FW = 288, PW = 4, HOLD = 200;
    localparam [3:0] T_WR_REQ = 4'h1, T_WR_ACK = 4'h3, T_WR_DATA = 4'h4;
    localparam [3:0] T_CU_INST = 4'h5, T_CU_SIGNAL = 4'h6;

    reg clk = 0, resetn = 0;
    always #2 clk = ~clk;

    reg  [FW-1:0] in_data = 0;
    reg           in_valid = 0;
    wire          in_busy;
    wire [FW-1:0] out_data;
    wire          out_valid;

    wire [FW-1:0] inst_flit, recv_flit;
    wire          inst_valid, recv_valid, send_ready;
    reg           inst_ready = 0, exec_done = 0;
    reg  [FW-1:0] send_flit = 0;
    reg           send_valid = 0;

    noc_cu_base #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(2), .POS_Y(1),
                  .ACK_FENCE(`FENCE)) dut (
        .clk(clk), .resetn(resetn),
        .noc_in_data(in_data), .noc_in_valid(in_valid), .noc_in_busy(in_busy),
        .noc_out_data(out_data), .noc_out_valid(out_valid), .noc_out_busy(1'b0),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(32'd0), .exec_fault(1'b0),
        .dbg_ctr(64'd0),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(1'b1),
        .inst_space(), .busy());

    function [FW-1:0] hdr(input [3:0] dx, input [3:0] dy, input [3:0] sx,
                          input [3:0] sy, input [3:0] ty, input last);
        hdr = {dx, dy, sx, sy, ty, 8'h07, last, 3'b000, {(FW-32){1'b0}}};
    endfunction

    integer cyc = 0, t_req = -1, t_ack = -1, t_sig = -1, errors = 0;
    always @(posedge clk) begin
        cyc <= cyc + 1;
        if (out_valid) begin
            if (out_data[FW-17 -: 4] == T_WR_REQ  && t_req < 0) t_req = cyc;
            if (out_data[FW-17 -: 4] == T_CU_SIGNAL && t_sig < 0) t_sig = cyc;
        end
    end

    // the datapath stub: take the instruction, send a 1-beat write, retire
    initial begin
        #20000;
        $display("FAIL -- cu_ack_fence: watchdog (req %0d sig %0d)", t_req, t_sig);
        $finish;
    end

    initial begin
        #20 resetn = 1;
        repeat (50) @(posedge clk);   // XPM FIFOs hold wr_rst_busy after reset
        // CU_INST from the node at (0,0)
        @(negedge clk); in_data = hdr(4'd2, 4'd1, 4'd0, 4'd0, T_CU_INST, 1'b1); in_valid = 1;
        @(negedge clk); in_valid = 0;
        wait (inst_valid);
        @(negedge clk); inst_ready = 1;
        @(negedge clk); inst_ready = 0;
        send_flit = hdr(4'd1, 4'd1, 4'd2, 4'd1, T_WR_REQ, 1'b0); send_valid = 1;
        @(posedge clk); while (!send_ready) @(posedge clk);
        @(negedge clk); send_flit = hdr(4'd1, 4'd1, 4'd2, 4'd1, T_WR_DATA, 1'b1);
        @(posedge clk); while (!send_ready) @(posedge clk);
        @(negedge clk); send_valid = 0;
        exec_done = 1;
        @(negedge clk); exec_done = 0;
        // the memory takes HOLD cycles to acknowledge
        repeat (HOLD) @(negedge clk);
        t_ack = cyc;
        in_data = hdr(4'd2, 4'd1, 4'd1, 4'd1, T_WR_ACK, 1'b1); in_valid = 1;
        @(negedge clk); while (in_busy) @(negedge clk);
        in_valid = 0;
        repeat (40) @(negedge clk);

        if (t_req < 0) begin errors = errors + 1; $display("FAIL: no MEM_WR_REQ left the unit"); end
        if (t_sig < 0) begin errors = errors + 1; $display("FAIL: no CU_SIGNAL left the unit"); end
        else if (t_sig <= t_ack) begin
            errors = errors + 1;
            $display("FAIL: CU_SIGNAL at cycle %0d, before the write ACK at %0d", t_sig, t_ack);
        end
        if (recv_valid) begin errors = errors + 1; $display("FAIL: the ACK reached the datapath"); end
        $display("  req %0d  ack %0d  signal %0d  (FENCE=%0d)", t_req, t_ack, t_sig, `FENCE);
        if (errors == 0) $display("PASS -- cu_ack_fence: signal waits for the write ACK");
        else $display("FAIL -- cu_ack_fence: %0d error(s)", errors);
        $finish;
    end
endmodule

`default_nettype wire
