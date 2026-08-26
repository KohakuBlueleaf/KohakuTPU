// A stand-in for xpm_memory_tdpram, used only under Verilator. See
// xpm_memory_sdpram.v for why the Xilinx source cannot be used directly.
//
// WRITE_MODE "no_change" on both ports, byte enables live, READ_LATENCY 1 --
// the slice rv_ram_be.v pins. rv_ram_be.v guards the true-dual-port collision
// itself, so this model leaves a collision undefined rather than resolving it.

`default_nettype none

module xpm_memory_tdpram #(
    parameter integer ADDR_WIDTH_A       = 10,
    parameter integer ADDR_WIDTH_B       = 10,
    parameter integer WRITE_DATA_WIDTH_A = 32,
    parameter integer WRITE_DATA_WIDTH_B = 32,
    parameter integer READ_DATA_WIDTH_A  = 32,
    parameter integer READ_DATA_WIDTH_B  = 32,
    parameter integer BYTE_WRITE_WIDTH_A = 8,
    parameter integer BYTE_WRITE_WIDTH_B = 8,
    parameter integer MEMORY_SIZE        = 32768,
    parameter         MEMORY_PRIMITIVE   = "block",
    parameter         CLOCKING_MODE      = "common_clock",
    parameter integer READ_LATENCY_A     = 1,
    parameter integer READ_LATENCY_B     = 1,
    parameter         WRITE_MODE_A       = "no_change",
    parameter         WRITE_MODE_B       = "no_change",
    parameter         MEMORY_INIT_FILE   = "none",
    parameter integer USE_MEM_INIT       = 0,
    parameter         ECC_MODE           = "no_ecc",
    parameter integer AUTO_SLEEP_TIME    = 0,
    parameter integer CASCADE_HEIGHT     = 0,
    parameter integer SIM_ASSERT_CHK     = 0,
    parameter integer USE_EMBEDDED_CONSTRAINT = 0,
    parameter         WAKEUP_TIME        = "disable_sleep"
)(
    input  wire                          clka,
    input  wire                          rsta,
    input  wire                          ena,
    input  wire                          regcea,
    input  wire [WRITE_DATA_WIDTH_A/BYTE_WRITE_WIDTH_A-1:0] wea,
    input  wire [ADDR_WIDTH_A-1:0]       addra,
    input  wire [WRITE_DATA_WIDTH_A-1:0] dina,
    output reg  [READ_DATA_WIDTH_A-1:0]  douta,

    input  wire                          clkb,
    input  wire                          rstb,
    input  wire                          enb,
    input  wire                          regceb,
    input  wire [WRITE_DATA_WIDTH_B/BYTE_WRITE_WIDTH_B-1:0] web,
    input  wire [ADDR_WIDTH_B-1:0]       addrb,
    input  wire [WRITE_DATA_WIDTH_B-1:0] dinb,
    output reg  [READ_DATA_WIDTH_B-1:0]  doutb,

    input  wire                          injectsbiterra,
    input  wire                          injectdbiterra,
    input  wire                          injectsbiterrb,
    input  wire                          injectdbiterrb,
    output wire                          sbiterra,
    output wire                          dbiterra,
    output wire                          sbiterrb,
    output wire                          dbiterrb,
    input  wire                          sleep
);
    localparam integer DEPTH = MEMORY_SIZE / WRITE_DATA_WIDTH_A;
    localparam integer NB    = WRITE_DATA_WIDTH_A / BYTE_WRITE_WIDTH_A;

    initial begin
        if (ECC_MODE != "no_ecc")
            $fatal(1, "xpm_memory_tdpram shim: ECC_MODE=%s not modelled", ECC_MODE);
        if (WRITE_MODE_A != "no_change" || WRITE_MODE_B != "no_change")
            $fatal(1, "xpm_memory_tdpram shim: WRITE_MODE other than no_change not modelled");
        if (READ_LATENCY_A != 1 || READ_LATENCY_B != 1)
            $fatal(1, "xpm_memory_tdpram shim: READ_LATENCY must be 1");
        if (USE_MEM_INIT != 0 || MEMORY_INIT_FILE != "none")
            $fatal(1, "xpm_memory_tdpram shim: memory init not modelled");
    end

    reg [WRITE_DATA_WIDTH_A-1:0] mem [0:DEPTH-1];
    integer i;

    always @(posedge clka) begin
        if (ena) begin
            if (|wea) begin
                for (i = 0; i < NB; i = i + 1)
                    if (wea[i])
                        mem[addra][i*BYTE_WRITE_WIDTH_A +: BYTE_WRITE_WIDTH_A]
                            <= dina[i*BYTE_WRITE_WIDTH_A +: BYTE_WRITE_WIDTH_A];
            end
            else if (rsta) douta <= {READ_DATA_WIDTH_A{1'b0}};
            else           douta <= mem[addra];
        end
    end

    always @(posedge clkb) begin
        if (enb) begin
            if (|web) begin
                for (i = 0; i < NB; i = i + 1)
                    if (web[i])
                        mem[addrb][i*BYTE_WRITE_WIDTH_B +: BYTE_WRITE_WIDTH_B]
                            <= dinb[i*BYTE_WRITE_WIDTH_B +: BYTE_WRITE_WIDTH_B];
            end
            else if (rstb) doutb <= {READ_DATA_WIDTH_B{1'b0}};
            else           doutb <= mem[addrb];
        end
    end

    assign sbiterra = 1'b0;
    assign dbiterra = 1'b0;
    assign sbiterrb = 1'b0;
    assign dbiterrb = 1'b0;

endmodule

`default_nettype wire
