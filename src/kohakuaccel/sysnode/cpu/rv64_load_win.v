// rv64_load_win -- the RV64 node's whole host-facing control window in a 4 KB
// (4hex) slot. NOT AXI: a plain register bus, no axi2lite tax. H_CTRL regs pass
// through at their own offsets; imem/spad are STREAMED (LOAD_CTL once, then pour
// into LOAD_DATA -- the pointer walks itself) so the 8 KB imem needs no mapped
// window; console bytes land in a FIFO read back at R_CONS.

`default_nettype none

module rv64_load_win #(
    parameter integer HS_AW = 32
)(
    input  wire              clk,
    input  wire              resetn,

    // ---- the compact register window (byte offsets inside 4 KB) ----
    input  wire              lb_en,
    input  wire              lb_wr,
    input  wire [11:0]       lb_addr,
    input  wire [63:0]       lb_wdata,
    input  wire [7:0]        lb_wstrb,
    output reg  [63:0]       lb_rdata,

    // ---- the RV64 host-load window ----
    output reg  [HS_AW-1:0]  hs_addr,
    output reg               hs_wr,
    output reg  [63:0]       hs_wdata,
    output reg  [7:0]        hs_wstrb,
    output reg               hs_rd,
    input  wire [63:0]       hs_rdata,

    // ---- console sideband from the node (the program's stdout) ----
    input  wire              hs_console_we,
    input  wire [7:0]        hs_console
);
    // hs_ region bases, selected by hs_addr[31:28].
    localparam [31:0] H_IMEM = 32'h0000_0000;
    localparam [31:0] H_SPAD = 32'h1000_0000;
    localparam [31:0] H_CTRL = 32'h2000_0000;

    // Window offsets. [0x00..0x7F] mirror the hs_ H_CTRL registers (boot/status/
    // exit/stdin) at their own offsets; the rest are local.
    localparam [11:0] R_LOADC = 12'h080;   // {region[0], start[31:8]}
    localparam [11:0] R_LOADD = 12'h088;   // stream a word into imem/spad
    localparam [11:0] R_CONS  = 12'h090;   // read {valid, byte}; a write pops

    reg         ld_region;                 // 0 = imem, 1 = spad
    reg  [15:0] ld_off;

    wire pass = lb_en && (lb_addr < 12'h080);
    wire load = lb_en && lb_wr && (lb_addr == R_LOADD);

    // ---- combinational hs_ drive (mirrors a bare hs_ access, no added latency) --
    always @(*) begin
        hs_addr  = {HS_AW{1'b0}};
        hs_wr    = 1'b0;
        hs_rd    = 1'b0;
        hs_wdata = lb_wdata;
        hs_wstrb = lb_wstrb;
        if (pass) begin
            hs_addr = H_CTRL | {20'd0, lb_addr};
            hs_wr   = lb_wr;
            hs_rd   = !lb_wr;
        end
        else if (load) begin
            hs_addr  = (ld_region ? H_SPAD : H_IMEM) | {16'd0, ld_off};
            hs_wr    = 1'b1;
            hs_wstrb = ld_region ? lb_wstrb : 8'h0f;
        end
    end

    // ---- the load pointer ----
    always @(posedge clk) begin
        if (!resetn) begin
            ld_region <= 1'b0;
            ld_off    <= 16'd0;
        end
        else begin
            if (lb_en && lb_wr && (lb_addr == R_LOADC)) begin
                ld_region <= lb_wdata[0];
                ld_off    <= lb_wdata[23:8];
            end
            else if (load) begin
                ld_off <= ld_off + (ld_region ? 16'd8 : 16'd4);
            end
        end
    end

    // ---- console FIFO (256 bytes) ----
    reg [7:0] con_mem [0:255];
    reg [7:0] con_wr, con_rd;
    wire      con_empty = (con_wr == con_rd);

    always @(posedge clk) begin
        if (!resetn) begin
            con_wr <= 8'd0;
            con_rd <= 8'd0;
        end
        else begin
            if (hs_console_we) begin
                con_mem[con_wr] <= hs_console;
                con_wr <= con_wr + 8'd1;
            end
            if (lb_en && lb_wr && (lb_addr == R_CONS) && !con_empty) begin
                con_rd <= con_rd + 8'd1;
            end
        end
    end

    // ---- readback ----
    always @(*) begin
        if (lb_addr < 12'h080) begin
            lb_rdata = hs_rdata;
        end
        else if (lb_addr == R_CONS) begin
            lb_rdata = {55'd0, !con_empty, con_mem[con_rd]};
        end
        else begin
            lb_rdata = 64'd0;
        end
    end

endmodule

`default_nettype wire
