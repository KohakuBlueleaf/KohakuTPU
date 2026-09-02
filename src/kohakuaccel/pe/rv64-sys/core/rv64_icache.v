// rv64_icache -- read-only I-cache over the node's cached range, so the core
// fetches code from DRAM, not only the on-chip window. It matches the window's
// contract: a hit answers one cycle after the address through a REGISTERED
// output, so the array-to-decode path ends at a register rather than running
// combinationally into the core's fetch and decode. A miss stalls; the fill
// costs one settle cycle so the registered word catches the new line. Small
// fully-associative, round-robin replacement, one outstanding miss. `inval`
// drops every line for FENCE.I.

`default_nettype none

module rv64_icache #(
    parameter integer LINES    = 4,           // fully-associative line buffers
    parameter integer ADDR_W   = 40,
    parameter         MEM_PRIM = "block",      // interface symmetry; unused here
    // 2: the word is two registers behind the address and both advance only
    // on `adv`, the core's fetch advance -- the shape of the instruction RAM's
    // READ_LAT 2 with REG_CE. 1: one register, reloaded every cycle.
    parameter integer LAT      = 1
)(
    input  wire                clk,
    input  wire                resetn,

    input  wire [ADDR_W-1:0]   fetch_pa,       // physical fetch address
    input  wire                en,             // fetch is in the cached range
    input  wire                adv,            // the fetch pipe advances (LAT 2)
    output wire [31:0]         idata,          // instruction, LAT cycles after a hit
    output wire                stall,           // hold fetch on a miss or fill settle

    input  wire                inval,          // FENCE.I: drop every line

    output reg                 if_req,
    input  wire                if_ready,
    output reg  [ADDR_W-6:0]   if_addr,        // line address
    input  wire                if_resp_valid,
    input  wire [255:0]        if_resp_data
);
    localparam integer LW = ADDR_W - 5;
    localparam integer VW = (LINES > 1) ? $clog2(LINES) : 1;

    reg [255:0]  cdata [0:LINES-1];
    reg [LW-1:0] ctag  [0:LINES-1];
    reg          cvld  [0:LINES-1];
    reg [VW-1:0] rr;

    wire [LW-1:0] f_line = fetch_pa[ADDR_W-1:5];

    integer i;
    reg hit;
    always @* begin
        hit = 1'b0;
        for (i = 0; i < LINES; i = i + 1) begin
            if (cvld[i] && (ctag[i] == f_line)) begin
                hit = 1'b1;
            end
        end
    end

    // A hit answers next cycle: the array-to-register path ends at idata_r, and
    // idata_r -> decode is a fresh register, so neither runs long into the core.
    reg [31:0] idata_r, idata_rr;
    wire       ld_r = (LAT == 2) ? adv : 1'b1;
    always @(posedge clk) begin
        if (ld_r) begin
            idata_r <= 32'd0;
            for (i = 0; i < LINES; i = i + 1) begin
                if (cvld[i] && (ctag[i] == f_line)) begin
                    idata_r <= cdata[i][{fetch_pa[4:2], 5'd0} +: 32];
                end
            end
        end
        if (adv) begin
            idata_rr <= idata_r;
        end
    end
    assign idata = (LAT == 2) ? idata_rr : idata_r;

    localparam [1:0] S_IDLE = 2'd0, S_REQ = 2'd1, S_WAIT = 2'd2, S_SETTLE = 2'd3;
    reg [1:0]    st;
    reg [LW-1:0] miss_line;

    // Miss, or the settle cycle after a fill (S_SETTLE) while idata_r catches up.
    assign stall = en && ((st != S_IDLE) || !hit);

    integer j;
    always @(posedge clk) begin
        if (!resetn) begin
            st     <= S_IDLE;
            if_req <= 1'b0;
            rr     <= {VW{1'b0}};
            for (j = 0; j < LINES; j = j + 1) begin
                cvld[j] <= 1'b0;
            end
        end
        else begin
            if (inval) begin
                for (j = 0; j < LINES; j = j + 1) begin
                    cvld[j] <= 1'b0;
                end
            end
            case (st)
                S_IDLE: if (en && !hit) begin
                    miss_line <= f_line;
                    if_addr   <= f_line;
                    if_req    <= 1'b1;
                    st        <= S_REQ;
                end
                S_REQ: if (if_req && if_ready) begin
                    if_req <= 1'b0;
                    st     <= S_WAIT;
                end
                S_WAIT: if (if_resp_valid) begin
                    cdata[rr] <= if_resp_data;
                    ctag[rr]  <= miss_line;
                    cvld[rr]  <= !inval;       // a fence mid-fill still drops it
                    rr        <= rr + 1'b1;
                    st        <= S_SETTLE;
                end
                S_SETTLE: st <= S_IDLE;
                default:  st <= S_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
