// A stand-in for xpm_memory_tdpram, used only under Verilator. See
// xpm_memory_sdpram.v for why the Xilinx source cannot be used directly.
//
// WRITE_MODE no_change (rv_ram_be.v) or read_first (sru_pool.v), READ_LATENCY 1
// or 2 (stage 2 = the regce/rst output register); a port collision is undefined.

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
    output wire [READ_DATA_WIDTH_A-1:0]  douta,

    input  wire                          clkb,
    input  wire                          rstb,
    input  wire                          enb,
    input  wire                          regceb,
    input  wire [WRITE_DATA_WIDTH_B/BYTE_WRITE_WIDTH_B-1:0] web,
    input  wire [ADDR_WIDTH_B-1:0]       addrb,
    input  wire [WRITE_DATA_WIDTH_B-1:0] dinb,
    output wire [READ_DATA_WIDTH_B-1:0]  doutb,

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
    localparam         RFA   = (WRITE_MODE_A == "read_first");
    localparam         RFB   = (WRITE_MODE_B == "read_first");

    initial begin
        if (ECC_MODE != "no_ecc")
            $fatal(1, "xpm_memory_tdpram shim: ECC_MODE=%s not modelled", ECC_MODE);
        if ((WRITE_MODE_A != "no_change" && !RFA) || (WRITE_MODE_B != "no_change" && !RFB))
            $fatal(1, "xpm_memory_tdpram shim: WRITE_MODE no_change or read_first only");
        if (READ_LATENCY_A < 1 || READ_LATENCY_A > 2 || READ_LATENCY_B < 1 || READ_LATENCY_B > 2)
            $fatal(1, "xpm_memory_tdpram shim: READ_LATENCY must be 1 or 2");
        if (USE_MEM_INIT != 0 || MEMORY_INIT_FILE != "none")
            $fatal(1, "xpm_memory_tdpram shim: memory init not modelled");
    end

    reg [WRITE_DATA_WIDTH_A-1:0] mem [0:DEPTH-1];
    reg [READ_DATA_WIDTH_A-1:0]  da1, da2;
    reg [READ_DATA_WIDTH_B-1:0]  db1, db2;
    integer i;

    always @(posedge clka) begin
        if (ena) begin
            if (|wea) begin
                for (i = 0; i < NB; i = i + 1)
                    if (wea[i])
                        mem[addra][i*BYTE_WRITE_WIDTH_A +: BYTE_WRITE_WIDTH_A]
                            <= dina[i*BYTE_WRITE_WIDTH_A +: BYTE_WRITE_WIDTH_A];
            end
            if (!(|wea) || RFA)
                da1 <= (rsta && READ_LATENCY_A == 1) ? {READ_DATA_WIDTH_A{1'b0}} : mem[addra];
        end
        if (rsta)        da2 <= {READ_DATA_WIDTH_A{1'b0}};
        else if (regcea) da2 <= da1;
    end
    assign douta = (READ_LATENCY_A == 2) ? da2 : da1;

    always @(posedge clkb) begin
        if (enb) begin
            if (|web) begin
                for (i = 0; i < NB; i = i + 1)
                    if (web[i])
                        mem[addrb][i*BYTE_WRITE_WIDTH_B +: BYTE_WRITE_WIDTH_B]
                            <= dinb[i*BYTE_WRITE_WIDTH_B +: BYTE_WRITE_WIDTH_B];
            end
            if (!(|web) || RFB)
                db1 <= (rstb && READ_LATENCY_B == 1) ? {READ_DATA_WIDTH_B{1'b0}} : mem[addrb];
        end
        if (rstb)        db2 <= {READ_DATA_WIDTH_B{1'b0}};
        else if (regceb) db2 <= db1;
    end
    assign doutb = (READ_LATENCY_B == 2) ? db2 : db1;

    assign sbiterra = 1'b0;
    assign dbiterra = 1'b0;
    assign sbiterrb = 1'b0;
    assign dbiterrb = 1'b0;

endmodule

`default_nettype wire
