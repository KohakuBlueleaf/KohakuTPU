// A stand-in for xpm_fifo_async, used only under Verilator. See
// xpm_memory_sdpram.v for why the Xilinx source cannot be used directly.
//
// Gray-coded pointers through real CDC_SYNC_STAGES flops, not a shortcut: the
// benches this feeds (mm_mesh_cdc, interlink_cdc_chain) exist to measure
// crossing latency, so collapsing the synchroniser would erase what they test.

`default_nettype none

module xpm_fifo_async #(
    parameter integer CASCADE_HEIGHT    = 0,
    parameter integer CDC_SYNC_STAGES   = 2,
    parameter         DOUT_RESET_VALUE  = "0",
    parameter         ECC_MODE          = "no_ecc",
    parameter         EN_SIM_ASSERT_ERR = "warning",
    parameter         FIFO_MEMORY_TYPE  = "distributed",
    parameter integer FIFO_READ_LATENCY = 0,
    parameter integer FIFO_WRITE_DEPTH  = 32,
    parameter integer FULL_RESET_VALUE  = 0,
    parameter integer PROG_EMPTY_THRESH = 5,
    parameter integer PROG_FULL_THRESH  = 5,
    parameter integer READ_DATA_WIDTH   = 288,
    parameter         READ_MODE         = "fwft",
    parameter integer RELATED_CLOCKS    = 0,
    parameter integer SIM_ASSERT_CHK    = 0,
    parameter [12:0]  USE_ADV_FEATURES  = 13'b0,
    parameter integer WAKEUP_TIME       = 0,
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
    input  wire [WRITE_DATA_WIDTH-1:0] din,
    input  wire                        rd_clk,
    input  wire                        rd_en,
    input  wire                        rst,
    input  wire                        wr_clk,
    input  wire                        wr_en,
    input  wire                        injectsbiterr,
    input  wire                        injectdbiterr,
    input  wire                        sleep
);
    // CAPACITY IS DEPTH+1 -- one, where the sync cell carries two. MEASURED on
    // sim/verilator/examples/vlt_async_fifo_tb.v: the Xilinx cell peaks at 33
    // words in flight at FIFO_WRITE_DEPTH 32, so this is not assumed symmetric
    // with xpm_fifo_sync. Getting it wrong either way breaks a credit scheme.
    localparam integer CAP  = FIFO_WRITE_DEPTH + 1;
    localparam integer AW   = $clog2(CAP);
    localparam integer MEMD = 1 << AW;

    initial begin
        if (READ_MODE != "fwft")
            $fatal(1, "xpm_fifo_async shim: READ_MODE=%s not modelled", READ_MODE);
        if (FIFO_READ_LATENCY != 0)
            $fatal(1, "xpm_fifo_async shim: FIFO_READ_LATENCY=%0d not modelled", FIFO_READ_LATENCY);
        if (ECC_MODE != "no_ecc")
            $fatal(1, "xpm_fifo_async shim: ECC_MODE=%s not modelled", ECC_MODE);
        if (USE_ADV_FEATURES != 13'b0)
            $fatal(1, "xpm_fifo_async shim: USE_ADV_FEATURES=%b not modelled", USE_ADV_FEATURES);
    end

    function automatic [AW:0] bin2gray(input [AW:0] b);
        bin2gray = b ^ (b >> 1);
    endfunction

    // Decoded in each domain so occupancy can be compared against a CAP that is
    // not a power of two; the CROSSING still carries gray, one bit at a time.
    function automatic [AW:0] gray2bin(input [AW:0] g);
        integer k;
        begin
            gray2bin[AW] = g[AW];
            for (k = AW - 1; k >= 0; k = k - 1)
                gray2bin[k] = gray2bin[k+1] ^ g[k];
        end
    endfunction

    reg [WRITE_DATA_WIDTH-1:0] mem [0:MEMD-1];

    reg [AW:0] wbin = {AW+1{1'b0}}, wgray = {AW+1{1'b0}};
    reg [AW:0] rbin = {AW+1{1'b0}}, rgray = {AW+1{1'b0}};

    // rst is wr_clk-domain and asynchronous to rd_clk, exactly as XPM has it.
    reg [AW:0] wg_meta [0:CDC_SYNC_STAGES-1];
    reg [AW:0] rg_meta [0:CDC_SYNC_STAGES-1];
    integer i;

    wire do_wr = wr_en && !full;
    wire do_rd = rd_en && !empty;

    always @(posedge wr_clk) begin
        if (rst) begin
            wbin  <= {AW+1{1'b0}};
            wgray <= {AW+1{1'b0}};
        end
        else if (do_wr) begin
            mem[wbin[AW-1:0]] <= din;
            wbin  <= wbin + 1'b1;
            wgray <= bin2gray(wbin + 1'b1);
        end
    end

    always @(posedge rd_clk) begin
        if (rst) begin
            rbin  <= {AW+1{1'b0}};
            rgray <= {AW+1{1'b0}};
        end
        else if (do_rd) begin
            rbin  <= rbin + 1'b1;
            rgray <= bin2gray(rbin + 1'b1);
        end
    end

    always @(posedge rd_clk) begin
        if (rst) for (i = 0; i < CDC_SYNC_STAGES; i = i + 1) wg_meta[i] <= {AW+1{1'b0}};
        else begin
            wg_meta[0] <= wgray;
            for (i = 1; i < CDC_SYNC_STAGES; i = i + 1) wg_meta[i] <= wg_meta[i-1];
        end
    end

    always @(posedge wr_clk) begin
        if (rst) for (i = 0; i < CDC_SYNC_STAGES; i = i + 1) rg_meta[i] <= {AW+1{1'b0}};
        else begin
            rg_meta[0] <= rgray;
            for (i = 1; i < CDC_SYNC_STAGES; i = i + 1) rg_meta[i] <= rg_meta[i-1];
        end
    end

    wire [AW:0] wgray_rd = wg_meta[CDC_SYNC_STAGES-1];
    wire [AW:0] rgray_wr = rg_meta[CDC_SYNC_STAGES-1];

    assign empty = (gray2bin(wgray_rd) == rbin);
    assign full  = ((wbin - gray2bin(rgray_wr)) == CAP[AW:0]);
    assign dout  = mem[rbin[AW-1:0]];

    reg wbusy = 1'b1;
    reg rbusy = 1'b1;
    always @(posedge wr_clk) wbusy <= rst;
    always @(posedge rd_clk) rbusy <= rst;

    assign wr_rst_busy = wbusy;
    assign rd_rst_busy = rbusy;
    assign prog_full   = 1'b0;
    assign prog_empty  = 1'b0;
    assign overflow    = 1'b0;
    assign underflow   = 1'b0;
    assign wr_ack      = 1'b0;
    assign data_valid  = 1'b0;

endmodule

`default_nettype wire
