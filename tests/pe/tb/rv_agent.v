// rv_agent -- the controller a PE bench needs, on one NoC endpoint.
//
// It is what the orchestrator would be in a real machine: it writes program
// images and data into a PE's windows as CU_DATA bursts, sends the CU_INST
// kick, and collects the CU_SIGNAL completion. Both the single-PE and the
// multi-core benches instantiate it and call its tasks hierarchically, so the
// boot sequence exists once.
//
// It NEVER APPLIES BACKPRESSURE inbound: a controller that can stop accepting
// completions is a controller that can wedge the mesh it is testing, and this
// bench is not the place to discover that.
//
// The image buffer is a plain array the bench fills with $readmemh before
// calling load_image, which is how a program written by tests/pe/tools/rv_asm.py
// reaches a PE without anything in between knowing what a program is.

`default_nettype none

module rv_agent #(
    parameter integer FW = 288,
    parameter integer PW = 4,
    parameter integer AX = 1,           // this agent's coordinates
    parameter integer AY = 0,
    parameter integer IMG_WORDS = 4096
)(
    input  wire          clk,
    input  wire          resetn,

    output reg  [FW-1:0] out_data,
    output reg           out_valid,
    input  wire          out_busy,

    input  wire [FW-1:0] in_data,
    input  wire          in_valid,
    output wire          in_busy
);
    localparam [3:0] T_CU_INST   = 4'h5;
    localparam [3:0] T_CU_SIGNAL = 4'h6;
    localparam [3:0] T_CU_DATA   = 4'h8;
    localparam integer PAY = FW - 4*PW - 16;

    assign in_busy = 1'b0;

    reg [31:0] img [0:IMG_WORDS-1];

    wire [3:0]  i_ty  = in_data[FW-4*PW-1 -: 4];
    wire [7:0]  i_txn = in_data[FW-4*PW-5 -: 8];
    wire [PW-1:0] i_sx = in_data[FW-2*PW-1 -: PW];
    wire [PW-1:0] i_sy = in_data[FW-3*PW-1 -: PW];

    // The most recent completion, and a running count so a bench can tell a
    // repeated signal from a missing one.
    reg [7:0]  sig_code, sig_txn;
    reg [31:0] sig_arg;
    reg [PW-1:0] sig_sx, sig_sy;
    integer    sig_n;
    integer    other_n;

    // ... and the same PER SOURCE at {y[1:0], x[1:0]}: completions arrive in
    // whatever order the programs finish, which the registers above cannot say.
    reg [7:0]  sig_code_at [0:15];
    reg [31:0] sig_arg_at  [0:15];
    integer    sig_n_at    [0:15];

    integer ai;
    initial begin
        out_data = {FW{1'b0}};
        out_valid = 1'b0;
        sig_n = 0;
        other_n = 0;
        for (ai = 0; ai < 16; ai = ai + 1) sig_n_at[ai] = 0;
    end

    always @(posedge clk) if (resetn && in_valid) begin
        if (i_ty == T_CU_SIGNAL) begin
            sig_code <= in_data[PAY-1 -: 8];
            sig_arg  <= in_data[PAY-9 -: 32];
            sig_txn  <= i_txn;
            sig_sx   <= i_sx;
            sig_sy   <= i_sy;
            sig_n    <= sig_n + 1;
            sig_code_at[{i_sy[1:0], i_sx[1:0]}] <= in_data[PAY-1 -: 8];
            sig_arg_at [{i_sy[1:0], i_sx[1:0]}] <= in_data[PAY-9 -: 32];
            sig_n_at   [{i_sy[1:0], i_sx[1:0]}] <=
                sig_n_at[{i_sy[1:0], i_sx[1:0]}] + 1;
        end else begin
            other_n <= other_n + 1;
            $display("%0t rv_agent: inbound flit type %h from (%0d,%0d) accepted and dropped",
                     $time, i_ty, i_sx, i_sy);
        end
    end

    // Hold until taken: valid stays asserted and the data unchanged until a
    // cycle in which the receiver is not busy. Withdrawing an offered flit
    // destroys it, silently, several modules from where it is noticed.
    task send(input [FW-1:0] f);
        integer guard;
        begin
            @(posedge clk);
            out_data  <= f;
            out_valid <= 1'b1;
            guard = 0;
            @(negedge clk);
            while (out_busy && (guard < 100000)) begin
                @(negedge clk);
                guard = guard + 1;
            end
            @(posedge clk);
            out_valid <= 1'b0;
            if (guard >= 100000)
                $display("%0t ERROR rv_agent: the link never accepted a flit",
                         $time);
        end
    endtask

    function [FW-1:0] hdr;
        input [PW-1:0] dx;
        input [PW-1:0] dy;
        input [3:0]    ty;
        input [7:0]    txn;
        input          lst;
        input [PAY-1:0] pay;
        hdr = {dx, dy, AX[PW-1:0], AY[PW-1:0], ty, txn, lst, 3'b000, pay};
    endfunction

    // ---- CU_DATA: raw 32-byte granules into a window ------------------------
    integer li_g, li_w, li_ng;
    reg [PAY-1:0] li_pl;
    // Sized explicitly: an integer expression inside a concatenation is 32 bits
    // whatever it holds, which silently widens the descriptor and shifts every
    // field below it.
    reg [15:0] li_off;
    reg [7:0]  li_len;
    task load_image(input [PW-1:0] dx, input [PW-1:0] dy,
                    input [7:0] buf_id, input integer nwords,
                    input integer word_off);
        begin
            li_ng  = (nwords + 7) / 8;
            li_off = word_off / 8;
            li_len = li_ng - 1;
            send(hdr(dx, dy, T_CU_DATA, 8'd0, 1'b0,
                     {buf_id, li_off, li_len, 8'd0, 4'd0, 4'd0, 208'd0}));
            for (li_g = 0; li_g < li_ng; li_g = li_g + 1) begin
                li_pl = {PAY{1'b0}};
                for (li_w = 0; li_w < 8; li_w = li_w + 1)
                    li_pl[li_w*32 +: 32] = img[li_g * 8 + li_w];
                send(hdr(dx, dy, T_CU_DATA, 8'd0, (li_g == li_ng - 1), li_pl));
            end
        end
    endtask

    // ---- CU_DATA: one 32-bit word with byte enables -------------------------
    // Exactly what a peer PE's `sw` becomes, so a bench pushing a doorbell and
    // a PE pushing one are the same event on the wire.
    task push_word(input [PW-1:0] dx, input [PW-1:0] dy, input [7:0] buf_id,
                   input [13:0] gran, input [2:0] sel, input [3:0] bena,
                   input [31:0] d);
        begin
            send(hdr(dx, dy, T_CU_DATA, 8'd0, 1'b0,
                     {buf_id, {2'd0, gran}, 8'd0, 8'd0, 4'd0, 4'd0, 208'd0}));
            send(hdr(dx, dy, T_CU_DATA, 8'd0, 1'b1,
                     {{(PAY-39){1'b0}}, sel, bena, d}));
        end
    endtask

    // ---- the kick -----------------------------------------------------------
    task kick(input [PW-1:0] dx, input [PW-1:0] dy, input [7:0] txn,
              input lst, input [7:0] op, input [31:0] pc0, input [31:0] arg);
        begin
            send(hdr(dx, dy, T_CU_INST, txn, lst,
                     {op, pc0, arg, {(PAY-72){1'b0}}}));
        end
    endtask

    integer ws_guard;
    task wait_signal(input integer want_n, input integer limit,
                     output integer ok);
        begin
            ws_guard = 0;
            while ((sig_n < want_n) && (ws_guard < limit)) begin
                @(posedge clk);
                ws_guard = ws_guard + 1;
            end
            ok = (sig_n >= want_n);
        end
    endtask

endmodule

`default_nettype wire
