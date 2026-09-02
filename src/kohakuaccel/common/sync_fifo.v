// Synchronous FIFO over xpm_fifo_sync.
//
// MEMORY_TYPE picks the storage primitive, because the right answer differs by
// use. Instruction FIFOs want "block": a RAMB36E2's widest shape is 512x72, so a
// 288-bit entry is 4 BRAMs and depth 512 fills them exactly.
//
// A 288-bit buffer is 4 RAMB36 at ANY depth to 512, so "block" for a router's
// in-port FIFO buys LUT with tiles it mostly leaves empty. Measured on one
// NoCRouter (2x2 grid, 3.333 ns ask): 512/"block" 3,052 LUT / 20 tiles /
// 386 MHz against 32/"distributed" 3,762 / 0 / 426 MHz -- 36 LUT per tile.
// Which side of that trade a mesh takes is its ROUTER_MEM parameter.

module sync_fifo #(
    parameter DATA_WIDTH        = 288,
    parameter FIFO_DEPTH        = 32,             // must be a power of 2
    parameter MEMORY_TYPE       = "distributed",  // "distributed" | "block" | "ultra"
    parameter PROG_FULL_THRESH  = FIFO_DEPTH - 5,
    parameter PROG_EMPTY_THRESH = 5
) (
    input  wire                     clk,
    input  wire                     rst,

    // Write interface
    input  wire                     wr_en,
    input  wire [DATA_WIDTH-1:0]    wr_data,
    output wire                     wr_busy,
    // NOT A MARGIN, despite the name and despite PROG_FULL_THRESH being passed:
    // USE_ADV_FEATURES below is zero, so XPM ties `prog_full` low and this
    // reduces to `wr_busy`. It never asserts early.
    //
    // Survivable only because the NoC link RETRIES -- sender holds `valid` until
    // a cycle with `busy` low, receiver accepts exactly then -- which needs no
    // margin (docs/noc/spec.md s2.1). Anything wanting a real margin must COUNT
    // FOR ITSELF, as MAG does with Q_MARGIN; turning the feature on means
    // editing USE_ADV_FEATURES, and nothing should depend on this bit until it
    // is.
    output wire                     wr_almost,

    // Read interface
    input  wire                     rd_en,
    output wire [DATA_WIDTH-1:0]    rd_data,
    output wire                     rd_busy
);
    generate if (MEMORY_TYPE == "lean") begin : g_lean
        // One inferred LUTRAM and one output register: xpm "distributed"
        // without its two output registers and control block.
        localparam integer AW = (FIFO_DEPTH <= 1) ? 1 : $clog2(FIFO_DEPTH);
        (* ram_style = "distributed" *) reg [DATA_WIDTH-1:0] mem [0:FIFO_DEPTH-1];
        reg [AW:0]            wp, rp;
        reg                   o_v;
        reg  [DATA_WIDTH-1:0] o_d;
        wire empty  = (wp == rp);
        wire full_w = (wp[AW-1:0] == rp[AW-1:0]) && (wp[AW] != rp[AW]);
        wire o_take = o_v && rd_en;
        wire issue  = !empty && (!o_v || o_take);
        wire wr_go  = wr_en && !full_w && !rst;
        always @(posedge clk) begin
            if (wr_go) begin mem[wp[AW-1:0]] <= wr_data; end
            if (rst) begin
                wp <= 0; rp <= 0; o_v <= 1'b0;
            end
            else begin
                if (wr_go) begin wp <= wp + 1'b1; end
                if (issue) begin rp <= rp + 1'b1; o_d <= mem[rp[AW-1:0]]; end
                o_v <= issue || (o_v && !o_take);
            end
        end
        assign wr_busy   = full_w | rst;
        assign wr_almost = wr_busy;
        assign rd_busy   = !o_v;
        assign rd_data   = o_d;
    end else begin : g_xpm
    wire rd_rst_busy, wr_rst_busy, empty, full, prog_full;

    // A LOCAL copy. The macro's own rst_busy sits with the FIFO memory, and
    // OR-ing it into wr_busy made it the top driver of failing control paths.
    reg rst_busy_q = 1'b1;
    always @(posedge clk) begin
        if (rst) begin
            rst_busy_q <= 1'b1;
        end
        else begin
            rst_busy_q <= wr_rst_busy | rd_rst_busy;
        end
    end

    assign wr_busy   = full | rst_busy_q;
    assign wr_almost = full | prog_full | rst_busy_q;
    assign rd_busy   = empty | rst_busy_q;

    xpm_fifo_sync #(
        .CASCADE_HEIGHT(0),
        .DOUT_RESET_VALUE("0"),
        .ECC_MODE("no_ecc"),
        .EN_SIM_ASSERT_ERR("warning"),
        .FIFO_MEMORY_TYPE(MEMORY_TYPE),
        .FIFO_READ_LATENCY(0),
        .FIFO_WRITE_DEPTH(FIFO_DEPTH),
        .FULL_RESET_VALUE(0),
        .PROG_EMPTY_THRESH(PROG_EMPTY_THRESH),
        .PROG_FULL_THRESH(PROG_FULL_THRESH),
        .READ_DATA_WIDTH(DATA_WIDTH),
        .READ_MODE("fwft"),
        .SIM_ASSERT_CHK(1),
        .USE_ADV_FEATURES(13'b0000000000000),
        .WRITE_DATA_WIDTH(DATA_WIDTH)
    )
    xpm_fifo_sync_inst (
        .dout(rd_data),
        .empty(empty),
        .full(full),
        .prog_full(prog_full),
        .rd_rst_busy(rd_rst_busy),
        .wr_rst_busy(wr_rst_busy),
        .din(wr_data),
        .rd_en(rd_en),
        .rst(rst),
        .wr_clk(clk),
        .wr_en(wr_en)
    );
    end endgenerate
endmodule
