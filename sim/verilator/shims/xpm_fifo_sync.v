// A stand-in for xpm_fifo_sync, used only under Verilator. See
// xpm_memory_sdpram.v for why the Xilinx source cannot be used directly.
//
// Models the slice sync_fifo.v pins: READ_MODE "fwft", FIFO_READ_LATENCY 0,
// no ECC, USE_ADV_FEATURES 0. `prog_full`/`prog_empty` are tied LOW because
// USE_ADV_FEATURES 0 is what XPM does -- sync_fifo.v's own comment depends on
// that exact behaviour for wr_almost.

`default_nettype none

module xpm_fifo_sync #(
    parameter integer CASCADE_HEIGHT    = 0,
    parameter         DOUT_RESET_VALUE  = "0",
    parameter         ECC_MODE          = "no_ecc",
    parameter         EN_SIM_ASSERT_ERR = "warning",
    parameter         FIFO_MEMORY_TYPE  = "distributed",
    parameter integer FIFO_READ_LATENCY = 0,
    parameter integer FIFO_WRITE_DEPTH  = 32,
    parameter integer FULL_RESET_VALUE  = 0,
    parameter integer PROG_EMPTY_THRESH = 5,
    parameter integer PROG_FULL_THRESH  = 27,
    parameter integer READ_DATA_WIDTH   = 288,
    parameter         READ_MODE         = "fwft",
    parameter integer SIM_ASSERT_CHK    = 0,
    parameter [12:0]  USE_ADV_FEATURES  = 13'b0,
    parameter integer WRITE_DATA_WIDTH  = 288
)(
    output wire [READ_DATA_WIDTH-1:0] dout,
    output wire                       empty,
    output wire                       full,
    output wire                       prog_full,
    output wire                       prog_empty,
    output wire                       rd_rst_busy,
    output wire                       wr_rst_busy,
    output wire                       overflow,
    output wire                       underflow,
    output wire                       wr_ack,
    output wire                       data_valid,
    output wire [$clog2(FIFO_WRITE_DEPTH):0] rd_data_count,
    output wire [$clog2(FIFO_WRITE_DEPTH):0] wr_data_count,
    input  wire [WRITE_DATA_WIDTH-1:0] din,
    input  wire                        rd_en,
    input  wire                        rst,
    input  wire                        wr_clk,
    input  wire                        wr_en,
    input  wire                        injectsbiterr,
    input  wire                        injectdbiterr,
    input  wire                        sleep
);
    localparam integer AW = $clog2(FIFO_WRITE_DEPTH);

    initial begin
        if (READ_MODE != "fwft") begin
            $fatal(1, "xpm_fifo_sync shim: READ_MODE=%s not modelled", READ_MODE);
        end
        if (FIFO_READ_LATENCY != 0) begin
            $fatal(1, "xpm_fifo_sync shim: FIFO_READ_LATENCY=%0d not modelled", FIFO_READ_LATENCY);
        end
        if (ECC_MODE != "no_ecc") begin
            $fatal(1, "xpm_fifo_sync shim: ECC_MODE=%s not modelled", ECC_MODE);
        end
        if (USE_ADV_FEATURES != 13'b0) begin
            $fatal(1, "xpm_fifo_sync shim: USE_ADV_FEATURES=%b not modelled", USE_ADV_FEATURES);
        end
    end

    // The array plus XPM's two fwft output stages (xpm_fifo.sv gen_fwft, READ_MODE
    // 1): stage 1 is the memory's read register, stage 2 the output register.
    // Capacity is therefore DEPTH+2, and a word written into an empty FIFO at
    // edge W is readable only from edge W+3 (ram_empty clears at W, the stage
    // FSM leaves `invalid` at W+1, `empty` clears at W+2); a pop before that is
    // ignored. Both are measured against the real cell by vlt_sync_fifo_tb.v.
    localparam integer INV = 0, S2 = 1, S1 = 2, BOTH = 3;

    reg [WRITE_DATA_WIDTH-1:0] mem [0:FIFO_WRITE_DEPTH-1];
    reg [31:0] wptr = 32'd0, rptr = 32'd0, cnt = 32'd0;
    reg [1:0]  st = INV;
    reg [READ_DATA_WIDTH-1:0] s1, s2 = {READ_DATA_WIDTH{1'b0}};
    reg        emp = 1'b1;

    wire ram_empty = (cnt == 32'd0);
    wire do_wr     = wr_en && !full;

    reg [1:0] nst;
    reg       ram_rd, regout;
    always @(*) begin
        case (st)
            INV: begin
                nst = ram_empty ? INV : S1;
                ram_rd = !ram_empty; regout = 1'b0;
            end
            S1: begin
                nst = ram_empty ? S2 : BOTH;
                ram_rd = !ram_empty; regout = 1'b1;
            end
            S2: begin
                nst = rd_en ? (ram_empty ? INV : S1) : (ram_empty ? S2 : BOTH);
                ram_rd = !ram_empty; regout = 1'b0;
            end
            default: begin
                nst = (ram_empty && rd_en) ? S2 : BOTH;
                ram_rd = !ram_empty && rd_en; regout = rd_en;
            end
        endcase
    end

    always @(posedge wr_clk) begin
        if (rst) begin
            wptr <= 32'd0;
            rptr <= 32'd0;
            cnt  <= 32'd0;
            st   <= INV;
            emp  <= 1'b1;
        end
        else begin
            if (do_wr) begin
                mem[wptr] <= din;
                wptr <= (wptr == FIFO_WRITE_DEPTH - 1) ? 32'd0 : wptr + 32'd1;
            end
            if (ram_rd) begin
                s1   <= mem[rptr];
                rptr <= (rptr == FIFO_WRITE_DEPTH - 1) ? 32'd0 : rptr + 32'd1;
            end
            if (regout) begin
                s2 <= s1;
            end
            cnt <= cnt + (do_wr ? 32'd1 : 32'd0) - (ram_rd ? 32'd1 : 32'd0);
            st  <= nst;
            emp <= ((st == S2) && rd_en) || ((st != S1) && emp);
        end
    end

    // XPM holds rst_busy for a few cycles after reset; sync_fifo.v registers it
    // into its own rst_busy_q, so a one-cycle model is enough to keep that path.
    reg busy = 1'b1;
    always @(posedge wr_clk) begin
        busy <= rst;
    end

    assign full  = (cnt == FIFO_WRITE_DEPTH);
    assign empty = emp;
    assign dout  = s2;

    assign wr_rst_busy = busy;
    assign rd_rst_busy = busy;
    assign prog_full   = 1'b0;
    assign prog_empty  = 1'b0;
    assign overflow    = 1'b0;
    assign underflow   = 1'b0;
    assign wr_ack      = 1'b0;
    assign data_valid  = 1'b0;
    assign rd_data_count = cnt[$clog2(FIFO_WRITE_DEPTH):0] + {{$clog2(FIFO_WRITE_DEPTH){1'b0}}, st[0]} + {{$clog2(FIFO_WRITE_DEPTH){1'b0}}, st[1]};
    assign wr_data_count = rd_data_count;

endmodule

`default_nettype wire
