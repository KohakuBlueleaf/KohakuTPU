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
    // CAPACITY IS DEPTH+1: the array fills at DEPTH-1 (XPM's going_full looks
    // two ahead) and the two fwft stages hold two more. MEASURED on
    // sim/verilator/examples/vlt_async_fifo_tb.v: the Xilinx cell peaks at 33
    // words in flight at FIFO_WRITE_DEPTH 32. Getting it wrong either way breaks
    // a credit scheme.
    localparam integer CAP  = FIFO_WRITE_DEPTH - 1;
    localparam integer AW   = $clog2(FIFO_WRITE_DEPTH + 1);
    localparam integer MEMD = 1 << AW;
    localparam integer INV = 0, S2 = 1, S1 = 2, BOTH = 3;

    initial begin
        if (READ_MODE != "fwft") begin
            $fatal(1, "xpm_fifo_async shim: READ_MODE=%s not modelled", READ_MODE);
        end
        if (FIFO_READ_LATENCY != 0) begin
            $fatal(1, "xpm_fifo_async shim: FIFO_READ_LATENCY=%0d not modelled", FIFO_READ_LATENCY);
        end
        if (ECC_MODE != "no_ecc") begin
            $fatal(1, "xpm_fifo_async shim: ECC_MODE=%s not modelled", ECC_MODE);
        end
        if (USE_ADV_FEATURES != 13'b0) begin
            $fatal(1, "xpm_fifo_async shim: USE_ADV_FEATURES=%b not modelled", USE_ADV_FEATURES);
        end
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
            for (k = AW - 1; k >= 0; k = k - 1) begin
                gray2bin[k] = gray2bin[k+1] ^ g[k];
            end
        end
    endfunction

    reg [WRITE_DATA_WIDTH-1:0] mem [0:MEMD-1];

    reg [AW:0] wbin = {AW+1{1'b0}}, wgray = {AW+1{1'b0}};
    reg [AW:0] rbin = {AW+1{1'b0}}, rgray = {AW+1{1'b0}};

    // rst is wr_clk-domain and asynchronous to rd_clk, exactly as XPM has it.
    reg [AW:0] wg_meta [0:CDC_SYNC_STAGES-1];
    reg [AW:0] rg_meta [0:CDC_SYNC_STAGES-1];
    integer i;
    wire [AW:0] wgray_rd = wg_meta[CDC_SYNC_STAGES-1];
    wire [AW:0] rgray_wr = rg_meta[CDC_SYNC_STAGES-1];

    wire do_wr = wr_en && !full;

    // Read side as xpm_fifo.sv: the synchronised write pointer is registered
    // (wpr_gray_reg), the array's empty flag is registered from it, and the
    // fwft stages behind it are the sync cell's FSM. A word written at a write
    // edge is readable four read edges after it reaches the synchroniser.
    reg  [AW:0] wbin_rd = {AW+1{1'b0}};
    reg         ram_empty = 1'b1;
    reg  [1:0]  st = INV;
    reg  [READ_DATA_WIDTH-1:0] s1, s2 = {READ_DATA_WIDTH{1'b0}};
    reg         emp = 1'b1;
    reg  [1:0]  nst;
    reg         do_rd, regout;
    always @(*) begin
        case (st)
            INV: begin
                nst = ram_empty ? INV : S1;
                do_rd = !ram_empty; regout = 1'b0;
            end
            S1: begin
                nst = ram_empty ? S2 : BOTH;
                do_rd = !ram_empty; regout = 1'b1;
            end
            S2: begin
                nst = rd_en ? (ram_empty ? INV : S1) : (ram_empty ? S2 : BOTH);
                do_rd = !ram_empty; regout = 1'b0;
            end
            default: begin
                nst = (ram_empty && rd_en) ? S2 : BOTH;
                do_rd = !ram_empty && rd_en; regout = rd_en;
            end
        endcase
    end

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
            rbin      <= {AW+1{1'b0}};
            rgray     <= {AW+1{1'b0}};
            wbin_rd   <= {AW+1{1'b0}};
            ram_empty <= 1'b1;
            st        <= INV;
            emp       <= 1'b1;
        end
        else begin
            if (do_rd) begin
                s1    <= mem[rbin[AW-1:0]];
                rbin  <= rbin + 1'b1;
                rgray <= bin2gray(rbin + 1'b1);
            end
            if (regout) begin
                s2 <= s1;
            end
            wbin_rd   <= gray2bin(wgray_rd);
            ram_empty <= (wbin_rd == rbin) || ((wbin_rd == rbin + 1'b1) && do_rd);
            st        <= nst;
            emp       <= ((st == S2) && rd_en) || ((st != S1) && emp);
        end
    end

    always @(posedge rd_clk) begin
        if (rst) begin
            for (i = 0; i < CDC_SYNC_STAGES; i = i + 1) begin
                wg_meta[i] <= {AW+1{1'b0}};
            end
        end
        else begin
            wg_meta[0] <= wgray;
            for (i = 1; i < CDC_SYNC_STAGES; i = i + 1) begin
                wg_meta[i] <= wg_meta[i-1];
            end
        end
    end

    always @(posedge wr_clk) begin
        if (rst) begin
            for (i = 0; i < CDC_SYNC_STAGES; i = i + 1) begin
                rg_meta[i] <= {AW+1{1'b0}};
            end
        end
        else begin
            rg_meta[0] <= rgray;
            for (i = 1; i < CDC_SYNC_STAGES; i = i + 1) begin
                rg_meta[i] <= rg_meta[i-1];
            end
        end
    end

    assign empty = emp;
    assign full  = ((wbin - gray2bin(rgray_wr)) == CAP[AW:0]);
    assign dout  = s2;

    reg wbusy = 1'b1;
    reg rbusy = 1'b1;
    always @(posedge wr_clk) begin
        wbusy <= rst;
    end
    always @(posedge rd_clk) begin
        rbusy <= rst;
    end

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
