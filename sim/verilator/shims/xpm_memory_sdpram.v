// A stand-in for xpm_memory_sdpram, used only under Verilator.
//
// The first word of this comment is not "Verilator": that spelling is parsed as
// a verilator metacomment and fails the build.
// Vivado's own xpm_memory.sv will not compile here: `xpm_memory_base` uses eight
// Verilog-1995 `deassign` statements and Verilator 5.020 rejects them outright.
// That module is the ONLY blocker, and xpm_fifo instantiates it too. Xilinx
// sources are neither copied nor patched -- this models the slice
// kohaku_sdpram.v actually uses, and $fatal-guards every mode it does not.
//
// `mem` is left uninitialised on purpose: zeroing it would hide the
// read-before-write bugs the benches exist to catch.

`default_nettype none

module xpm_memory_sdpram #(
    parameter integer ADDR_WIDTH_A       = 6,
    parameter integer ADDR_WIDTH_B       = 6,
    parameter integer WRITE_DATA_WIDTH_A = 32,
    parameter integer READ_DATA_WIDTH_B  = 32,
    parameter integer BYTE_WRITE_WIDTH_A = 32,
    parameter integer MEMORY_SIZE        = 2048,
    parameter         MEMORY_PRIMITIVE   = "block",
    parameter         CLOCKING_MODE      = "common_clock",
    parameter integer READ_LATENCY_B     = 1,
    parameter         WRITE_MODE_B       = "read_first",
    parameter         MEMORY_INIT_FILE   = "none",
    parameter integer USE_MEM_INIT       = 0,
    parameter         ECC_MODE           = "no_ecc",
    parameter integer AUTO_SLEEP_TIME    = 0,
    parameter integer CASCADE_HEIGHT     = 0,
    parameter integer SIM_ASSERT_CHK     = 0,
    parameter         WAKEUP_TIME        = "disable_sleep"
)(
    input  wire                          clka,
    input  wire                          ena,
    input  wire [WRITE_DATA_WIDTH_A/BYTE_WRITE_WIDTH_A-1:0] wea,
    input  wire [ADDR_WIDTH_A-1:0]       addra,
    input  wire [WRITE_DATA_WIDTH_A-1:0] dina,

    input  wire                          clkb,
    input  wire                          enb,
    input  wire [ADDR_WIDTH_B-1:0]       addrb,
    output wire [READ_DATA_WIDTH_B-1:0]  doutb,
    input  wire                          rstb,
    input  wire                          regceb,

    input  wire                          injectsbiterra,
    input  wire                          injectdbiterra,
    output wire                          sbiterrb,
    output wire                          dbiterrb,
    input  wire                          sleep
);
    localparam integer DEPTH = MEMORY_SIZE / WRITE_DATA_WIDTH_A;

    // REFUSED, NOT APPROXIMATED. A mode this model does not implement must stop
    // the run; simulating it as though it were `read_first` would produce a
    // plausible waveform that does not match silicon.
    initial begin
        if (ECC_MODE != "no_ecc")
            $fatal(1, "xpm_memory_sdpram shim: ECC_MODE=%s not modelled", ECC_MODE);
        if (WRITE_MODE_B != "read_first")
            $fatal(1, "xpm_memory_sdpram shim: WRITE_MODE_B=%s not modelled", WRITE_MODE_B);
        if (USE_MEM_INIT != 0 || MEMORY_INIT_FILE != "none")
            $fatal(1, "xpm_memory_sdpram shim: memory init not modelled");
        if (READ_LATENCY_B > 8)
            $fatal(1, "xpm_memory_sdpram shim: READ_LATENCY_B=%0d not modelled", READ_LATENCY_B);
    end

    reg [WRITE_DATA_WIDTH_A-1:0] mem [0:DEPTH-1];

    // One write enable per BYTE_WRITE_WIDTH_A lane, which is the whole word
    // when the two widths are equal.
    localparam integer NB = WRITE_DATA_WIDTH_A / BYTE_WRITE_WIDTH_A;
    integer i;
    always @(posedge clka) begin
        if (ena) begin
            for (i = 0; i < NB; i = i + 1) begin
                if (wea[i]) begin
                    mem[addra][i*BYTE_WRITE_WIDTH_A +: BYTE_WRITE_WIDTH_A]
                        <= dina[i*BYTE_WRITE_WIDTH_A +: BYTE_WRITE_WIDTH_A];
                end
            end
        end
    end

    // READ_FIRST: the value read is the one held BEFORE any same-cycle write,
    // which a separate always block reading `mem` gives for free -- both are
    // non-blocking against the same clock edge.
    generate
        if (READ_LATENCY_B == 0) begin : g_lat0
            assign doutb = mem[addrb];
        end
        else begin : g_lat
            reg [READ_DATA_WIDTH_B-1:0] q1;
            always @(posedge clkb) begin
                if (rstb)     q1 <= {READ_DATA_WIDTH_B{1'b0}};
                else if (enb) q1 <= mem[addrb];
            end
            if (READ_LATENCY_B == 1) begin : g_lat1
                assign doutb = q1;
            end
            else begin : g_latn
                // The optional output registers, all gated by regceb: one for a
                // block RAM, up to three more for a URAM cascade (READ_LAT 4 in
                // kx_carray) -- what the real cell absorbs as pipeline stages.
                reg [READ_DATA_WIDTH_B-1:0] qn [1:READ_LATENCY_B-1];
                integer s;
                always @(posedge clkb) begin
                    if (rstb) begin
                        for (s = 1; s < READ_LATENCY_B; s = s + 1) qn[s] <= {READ_DATA_WIDTH_B{1'b0}};
                    end else if (regceb) begin
                        qn[1] <= q1;
                        for (s = 2; s < READ_LATENCY_B; s = s + 1) qn[s] <= qn[s-1];
                    end
                end
                assign doutb = qn[READ_LATENCY_B-1];
            end
        end
    endgenerate

    assign sbiterrb = 1'b0;
    assign dbiterrb = 1'b0;

endmodule

`default_nettype wire
