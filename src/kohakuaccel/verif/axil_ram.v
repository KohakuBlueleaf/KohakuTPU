// AXI4-Lite slave RAM, 32-bit, one transaction at a time. Stands in for the
// two register endpoints on every die of multimesh_v8t that the simulated card
// has no IP for: the DDR4 controller's C0_DDR4_S_AXI_CTRL and the clock
// wizard's s_axi_lite. INIT_IDX/INIT_VAL preset ONE word at reset so a register
// the host polls (the wizard's STATUS, LOCKED = 1 at 0x004) answers.

`default_nettype none

module axil_ram #(
    parameter integer AW       = 43,
    parameter integer WORDS    = 256,
    parameter integer INIT_IDX = -1,
    parameter [31:0]  INIT_VAL = 32'd0
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
    input  wire          s_rready
);
    localparam integer IW = $clog2(WORDS);

    reg [31:0] mem [0:WORDS-1];

    reg        aw_got, w_got, bvalid_r;
    reg [IW-1:0] waddr;
    reg [31:0] wdata_r;
    reg [3:0]  wstrb_r;
    integer i;

    assign s_awready = !aw_got && !bvalid_r;
    assign s_wready  = !w_got  && !bvalid_r;
    assign s_bvalid  = bvalid_r;
    assign s_bresp   = 2'b00;

    always @(posedge clk) begin
        if (!resetn) begin
            aw_got <= 1'b0; w_got <= 1'b0; bvalid_r <= 1'b0;
            if (INIT_IDX >= 0) begin
                mem[INIT_IDX] <= INIT_VAL;
            end
        end else begin
            if (s_awvalid && s_awready) begin aw_got <= 1'b1; waddr <= s_awaddr[2 +: IW]; end
            if (s_wvalid  && s_wready)  begin w_got  <= 1'b1; wdata_r <= s_wdata; wstrb_r <= s_wstrb; end
            if (aw_got && w_got && !bvalid_r) begin
                for (i = 0; i < 4; i = i + 1) begin
                    if (wstrb_r[i]) begin
                        mem[waddr][i*8 +: 8] <= wdata_r[i*8 +: 8];
                    end
                end
                aw_got <= 1'b0; w_got <= 1'b0; bvalid_r <= 1'b1;
            end
            if (bvalid_r && s_bready) begin
                bvalid_r <= 1'b0;
            end
        end
    end

    reg        rvalid_r;
    reg [31:0] rdata_r;
    assign s_arready = !rvalid_r;
    assign s_rvalid  = rvalid_r;
    assign s_rdata   = rdata_r;
    assign s_rresp   = 2'b00;
    always @(posedge clk) begin
        if (!resetn) begin
            rvalid_r <= 1'b0;
        end
        else begin
            if (s_arvalid && s_arready) begin rdata_r <= mem[s_araddr[2 +: IW]]; rvalid_r <= 1'b1; end
            if (rvalid_r && s_rready) begin
                rvalid_r <= 1'b0;
            end
        end
    end
endmodule

`default_nettype wire
