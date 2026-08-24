// mv_exec -- the memory mover as an execution unit of the control processor.
//
// `mv.go rs1` hands over a POINTER; this fetches the descriptor from the
// processor's scratchpad and drives mm_mover's cfg port itself. mm_mover is
// unchanged -- the control merges, the datapath does not
// (docs/arch/sysnode/control-processor.md s4).
//
// The descriptor is what mover.py already emits, laid out as 32-bit words:
//
//     word 0        : n, the number of register writes
//     then n times  : {24'b0, offset[7:0]}, value[31:0], value[63:32]
//
// So the compiler emits descriptors as plain data built with ordinary stores,
// and program order is the queue: there is no ring buffer and no doorbell
// between processor and mover.

`default_nettype none

module mv_exec #(
    parameter integer SAW = 11              // scratchpad word-address width
)(
    input  wire             clk,
    input  wire             resetn,

    // ---- the instruction ----
    input  wire             go,             // one cycle, with `ptr` valid
    input  wire [SAW-1:0]   ptr,
    output wire             busy,           // mv.wait stalls on this

    // ---- scratchpad read port: address out, data one cycle later ----
    output reg              sp_req,
    output reg  [SAW-1:0]   sp_addr,
    input  wire [31:0]      sp_data,

    // ---- mm_mover's config port ----
    output reg              cfg_en,
    output reg  [7:0]       cfg_addr,
    output reg  [63:0]      cfg_data,

    // ---- the engine's own state, so `busy` spans the whole move ----
    input  wire             mv_busy
);
    localparam [2:0] S_IDLE = 3'd0, S_N = 3'd1, S_OFF = 3'd2, S_LO = 3'd3;
    localparam [2:0] S_HI = 3'd4, S_RUN = 3'd5;

    reg [2:0]     st;
    reg [15:0]    left;
    reg [SAW-1:0] cur;
    reg [7:0]     off_r;
    reg [31:0]    lo_r;
    reg           fetched;                  // sp_data for `sp_addr` is valid now

    // Not just the fetch: a move is in flight until the engine drops mv_busy,
    // and mv.wait has to cover both or it releases on a half-issued descriptor.
    assign busy = (st != S_IDLE) || mv_busy;

    always @(posedge clk) begin
        if (!resetn) begin
            st <= S_IDLE; sp_req <= 1'b0; cfg_en <= 1'b0; fetched <= 1'b0;
        end else begin
            cfg_en  <= 1'b0;
            sp_req  <= 1'b0;
            fetched <= sp_req;

            case (st)
                S_IDLE: if (go) begin
                    cur     <= ptr;
                    sp_addr <= ptr;
                    sp_req  <= 1'b1;
                    st      <= S_N;
                end

                S_N: if (fetched) begin
                    left    <= sp_data[15:0];
                    cur     <= cur + 1'b1;
                    sp_addr <= cur + 1'b1;
                    sp_req  <= 1'b1;
                    st      <= (sp_data[15:0] == 16'd0) ? S_IDLE : S_OFF;
                end

                S_OFF: if (fetched) begin
                    off_r   <= sp_data[7:0];
                    cur     <= cur + 1'b1;
                    sp_addr <= cur + 1'b1;
                    sp_req  <= 1'b1;
                    st      <= S_LO;
                end

                S_LO: if (fetched) begin
                    lo_r    <= sp_data;
                    cur     <= cur + 1'b1;
                    sp_addr <= cur + 1'b1;
                    sp_req  <= 1'b1;
                    st      <= S_HI;
                end

                S_HI: if (fetched) begin
                    cfg_addr <= off_r;
                    cfg_data <= {sp_data, lo_r};
                    cfg_en   <= 1'b1;
                    cur      <= cur + 1'b1;
                    left     <= left - 16'd1;
                    if (left == 16'd1) begin
                        st <= S_RUN;
                    end
                    else begin
                        sp_addr <= cur + 1'b1;
                        sp_req  <= 1'b1;
                        st      <= S_OFF;
                    end
                end

                // The GO write was the last one issued; hold until the engine picks
                // it up, or `busy` would fall between the pulse and mv_busy rising.
                S_RUN: begin
                    if (mv_busy) begin
                        st <= S_IDLE;
                    end
                end

                default: st <= S_IDLE;
            endcase
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (resetn && go && busy) begin
            $display("%0t ERROR mv_exec: mv.go while busy -- mv.wait was skipped",
                     $time);
        end
    end
`endif
endmodule

`default_nettype wire
