// Clocking Wizard stand-in: the PG065 AXI4-Lite register map of one DRP-capable
// MMCM, for the simulated card. 32-bit, one transaction at a time.
//
//   0x004 STATUS    bit 0 = locked
//   0x200 CLKCFG0   [7:0] DIVCLK_DIVIDE, [15:8] CLKFBOUT_MULT
//   0x208 + 12n     CLKOUTn divide (n = 0..6); +4 phase; +8 duty
//   0x25C LOAD      bit 0 with bit 1: latch the staged set, self-clearing
//
// Duty registers read 0; a full-word zero written to one answers SLVERR, as on the IP.
// LOAD drops `locked` for LOCK_CYC clk cycles; `mult`, `divclk`, `div` carry the applied set.

`default_nettype none

module clk_wiz_model #(
    parameter integer AW       = 43,
    parameter integer D        = 4,
    parameter integer M        = 48,
    parameter [8*4-1:0] DIV0   = {8'd4, 8'd4, 8'd2, 8'd4},   // CLKOUT3..0
    parameter integer LOCK_CYC = 64
)(
    input  wire          clk,
    input  wire          resetn,

    input  wire [AW-1:0] s_awaddr,
    input  wire          s_awvalid,
    output wire          s_awready,
    input  wire [31:0]   s_wdata,
    input  wire [3:0]    s_wstrb,
    input  wire          s_wvalid,
    output wire          s_wready,
    output wire [1:0]    s_bresp,
    output wire          s_bvalid,
    input  wire          s_bready,
    input  wire [AW-1:0] s_araddr,
    input  wire          s_arvalid,
    output wire          s_arready,
    output wire [31:0]   s_rdata,
    output wire [1:0]    s_rresp,
    output wire          s_rvalid,
    input  wire          s_rready,

    output wire          locked,
    output reg  [7:0]    divclk,
    output reg  [7:0]    mult,
    output reg  [8*4-1:0] div        // CLKOUT3..0 as applied
);
    reg [31:0] mem [0:255];          // staged words 0x000..0x3FC
    reg [15:0] lock_cnt;
    assign locked = (lock_cnt == 16'd0);

    function is_duty(input [7:0] w);
        is_duty = (w >= 8'h84) && (w <= 8'h96) && ((w - 8'h84) % 3 == 0);
    endfunction

    reg        aw_got, w_got, bvalid_r;
    reg [1:0]  bresp_r;
    reg [7:0]  waddr;
    reg [31:0] wdata_r;
    reg [3:0]  wstrb_r;
    wire [31:0] wmask = {{8{wstrb_r[3]}}, {8{wstrb_r[2]}}, {8{wstrb_r[1]}}, {8{wstrb_r[0]}}};
    integer i;
    initial for (i = 0; i < 256; i = i + 1) mem[i] = 32'd0;

    assign s_awready = !aw_got && !bvalid_r;
    assign s_wready  = !w_got  && !bvalid_r;
    assign s_bvalid  = bvalid_r;
    assign s_bresp   = bresp_r;

    always @(posedge clk) begin
        if (!resetn) begin
            aw_got <= 1'b0; w_got <= 1'b0; bvalid_r <= 1'b0; bresp_r <= 2'b00;
            lock_cnt <= 16'd0;
            mem[8'h80] <= (M << 8) | D;
            mem[8'h82] <= DIV0[7:0];
            mem[8'h85] <= DIV0[15:8];
            mem[8'h88] <= DIV0[23:16];
            mem[8'h8B] <= DIV0[31:24];
            divclk <= D; mult <= M; div <= DIV0;
        end else begin
            if (lock_cnt != 16'd0) lock_cnt <= lock_cnt - 16'd1;
            if (s_awvalid && s_awready) begin aw_got <= 1'b1; waddr <= s_awaddr[9:2]; end
            if (s_wvalid  && s_wready)  begin w_got  <= 1'b1; wdata_r <= s_wdata; wstrb_r <= s_wstrb; end
            if (aw_got && w_got && !bvalid_r) begin
                aw_got <= 1'b0; w_got <= 1'b0; bvalid_r <= 1'b1; bresp_r <= 2'b00;
                if (is_duty(waddr) && wdata_r == 32'd0 && wstrb_r == 4'hF) begin
                    bresp_r <= 2'b10;
                end else if (waddr == 8'h97) begin
                    if (wdata_r[0] && wdata_r[1]) begin
                        divclk <= mem[8'h80][7:0];
                        mult   <= mem[8'h80][15:8];
                        div    <= {mem[8'h8B][7:0], mem[8'h88][7:0], mem[8'h85][7:0], mem[8'h82][7:0]};
                        lock_cnt <= LOCK_CYC;
                    end
                end else if (waddr != 8'h01) begin
                    mem[waddr] <= (mem[waddr] & ~wmask) | (wdata_r & wmask);
                end
            end
            if (bvalid_r && s_bready) bvalid_r <= 1'b0;
        end
    end

    reg        rvalid_r;
    reg [31:0] rdata_r;
    wire [7:0] raddr = s_araddr[9:2];
    assign s_arready = !rvalid_r;
    assign s_rvalid  = rvalid_r;
    assign s_rdata   = rdata_r;
    assign s_rresp   = 2'b00;
    always @(posedge clk) begin
        if (!resetn) begin
            rvalid_r <= 1'b0;
        end else begin
            if (s_arvalid && s_arready) begin
                rvalid_r <= 1'b1;
                rdata_r  <= raddr == 8'h01 ? {31'd0, locked} :
                            is_duty(raddr) ? 32'd0 :
                            raddr == 8'h97 ? 32'd0 : mem[raddr];
            end
            if (rvalid_r && s_rready) rvalid_r <= 1'b0;
        end
    end
endmodule

`default_nettype wire
