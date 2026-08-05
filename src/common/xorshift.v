// Xorshift64 implementation
module xorshift64 (
    input wire clk,
    input wire rst,
    input wire en,
    input wire [63:0] seed,
    output reg [63:0] rand_out,
    output reg valid
);

    // Internal state
    reg [63:0] state;
    reg [1:0] stage;
    
    // Xorshift64 algorithm
    always @(posedge clk) begin
        if (rst) begin
            state <= seed;
            stage <= 0;
            valid <= 0;
        end else if (en) begin
            case (stage)
                0: begin
                    state <= state ^ (state << 13);
                    stage <= 1;
                    valid <= 0;
                end
                1: begin
                    state <= state ^ (state >> 7);
                    stage <= 2;
                end
                2: begin
                    state <= state ^ (state << 17);
                    stage <= 3;
                end
                3: begin
                    rand_out <= state;
                    valid <= 1;
                    stage <= 0;
                end
            endcase
        end else begin
            valid <= 0;
        end
    end
endmodule


// Single cycle 128-bit xorshift
module xorshift128_single (
    input wire clk,
    input wire rst,
    input wire en,
    input wire [127:0] seed,
    output reg [127:0] rand_out
);
    // Internal state
    reg [63:0] state0, state1;
    wire [63:0] next_state0, next_state1;
    
    // Compute next states combinationally
    assign next_state0 = state1;
    assign next_state1 = state0 ^ ((state0 << 23) ^ (state0 >> 17) ^ state1 ^ (state1 >> 26));
    
    // Update state and output
    always @(posedge clk) begin
        if (rst) begin
            state0 <= seed[63:0];
            state1 <= seed[127:64];
            rand_out <= seed;
        end else if (en) begin
            state0 <= next_state0;
            state1 <= next_state1;
            rand_out <= {next_state0, next_state1};
        end
    end
endmodule


// Single cycle 256-bit xorshift
module xorshift256 (
    input wire clk,
    input wire rst,
    input wire en,
    input wire [255:0] seed,
    output wire [255:0] rand_out
);
    // Internal state - we use two 128-bit generators with different parameters
    reg [63:0] state0, state1, state2, state3;
    wire [63:0] next_state0, next_state1, next_state2, next_state3;
    
    // First 128-bit generator
    assign next_state0 = state1;
    assign next_state1 = state0 ^ ((state0 << 23) ^ (state0 >> 17) ^ state1 ^ (state1 >> 26));
    
    // Second 128-bit generator (different shift values for better independence)
    assign next_state2 = state3;
    assign next_state3 = state2 ^ ((state2 << 19) ^ (state2 >> 11) ^ state3 ^ (state3 >> 29));
    
    // Update state and output
    always @(posedge clk) begin
        if (rst) begin
            state0 <= seed[63:0];
            state1 <= seed[127:64];
            state2 <= seed[191:128];
            state3 <= seed[255:192];
        end else if (en) begin
            state0 <= next_state0;
            state1 <= next_state1;
            state2 <= next_state2;
            state3 <= next_state3;
        end
    end
    
    // Combine outputs from both generators
    assign rand_out = {next_state2, next_state3, next_state0, next_state1};
endmodule