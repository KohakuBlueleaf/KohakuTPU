`timescale 1ns / 1ps

module InstReceiver_tb();
    // Parameters
    parameter INSTRUCTION_DEPTH = 16;
    parameter DATA_WIDTH = 64;
    parameter ADDR_WIDTH = 64;
    parameter ID_WIDTH = 4;
    parameter STRB_WIDTH = DATA_WIDTH/8;
    
    // Clock generation
    reg clk = 0;
    always #5 clk = ~clk;
    
    // Reset generation
    reg rst;
    
    // AXI Write Address Channel
    reg [ADDR_WIDTH-1:0] S_AXI_AWADDR;
    reg [ID_WIDTH-1:0] S_AXI_AWID;
    reg [7:0] S_AXI_AWLEN;
    reg [2:0] S_AXI_AWSIZE;
    reg [1:0] S_AXI_AWBURST;
    reg S_AXI_AWVALID;
    wire S_AXI_AWREADY;
    
    // AXI Write Data Channel
    reg [DATA_WIDTH-1:0] S_AXI_WDATA;
    reg [STRB_WIDTH-1:0] S_AXI_WSTRB;
    reg S_AXI_WLAST;
    reg S_AXI_WVALID;
    wire S_AXI_WREADY;
    
    // AXI Write Response Channel
    wire [ID_WIDTH-1:0] S_AXI_BID;
    wire [1:0] S_AXI_BRESP;
    wire S_AXI_BVALID;
    reg S_AXI_BREADY;
    
    // Unused read channels - tied to 0
    wire [ID_WIDTH-1:0] S_AXI_RID;
    wire [DATA_WIDTH-1:0] S_AXI_RDATA;
    wire [1:0] S_AXI_RRESP;
    wire S_AXI_RLAST;
    wire S_AXI_RVALID;
    wire S_AXI_ARREADY;
    
    // Instruction interface
    wire [DATA_WIDTH-1:0] instruction;
    wire [$clog2(INSTRUCTION_DEPTH)-1:0] instruction_id;
    wire instruction_valid;
    reg instruction_next;
    
    // DUT instantiation
    InstReceiver #(
        .INSTRUCTION_DEPTH(INSTRUCTION_DEPTH),
        .DATA_WIDTH(DATA_WIDTH),
        .ADDR_WIDTH(ADDR_WIDTH),
        .ID_WIDTH(ID_WIDTH)
    ) dut (
        .clk(clk),
        .rst(rst),
        
        // Write Address Channel
        .S_AXI_AWADDR(S_AXI_AWADDR),
        .S_AXI_AWID(S_AXI_AWID),
        .S_AXI_AWLEN(S_AXI_AWLEN),
        .S_AXI_AWSIZE(S_AXI_AWSIZE),
        .S_AXI_AWBURST(S_AXI_AWBURST),
        .S_AXI_AWVALID(S_AXI_AWVALID),
        .S_AXI_AWREADY(S_AXI_AWREADY),
        
        // Write Data Channel
        .S_AXI_WDATA(S_AXI_WDATA),
        .S_AXI_WSTRB(S_AXI_WSTRB),
        .S_AXI_WLAST(S_AXI_WLAST),
        .S_AXI_WVALID(S_AXI_WVALID),
        .S_AXI_WREADY(S_AXI_WREADY),
        
        // Write Response Channel
        .S_AXI_BID(S_AXI_BID),
        .S_AXI_BRESP(S_AXI_BRESP),
        .S_AXI_BVALID(S_AXI_BVALID),
        .S_AXI_BREADY(S_AXI_BREADY),
        
        // Read Address Channel (unused)
        .S_AXI_ARADDR(64'h0),
        .S_AXI_ARID(4'h0),
        .S_AXI_ARLEN(8'h0),
        .S_AXI_ARSIZE(3'h0),
        .S_AXI_ARBURST(2'h0),
        .S_AXI_ARVALID(1'b0),
        .S_AXI_ARREADY(S_AXI_ARREADY),
        
        // Read Data Channel (unused)
        .S_AXI_RID(S_AXI_RID),
        .S_AXI_RDATA(S_AXI_RDATA),
        .S_AXI_RRESP(S_AXI_RRESP),
        .S_AXI_RLAST(S_AXI_RLAST),
        .S_AXI_RVALID(S_AXI_RVALID),
        .S_AXI_RREADY(1'b0),
        
        // Instruction interface
        .instruction(instruction),
        .instruction_id(instruction_id),
        .instruction_valid(instruction_valid),
        .instruction_next(instruction_next),
        
        // Processing State (unused)
        .data(64'h0),
        .data_id(4'h0),
        .data_valid(1'b0)
    );
    
    integer i;
    // Test stimulus
    initial begin
        // Initialize VCD dump
        $dumpfile("inst_receiver_tb.vcd");
        $dumpvars(0, InstReceiver_tb);
        
        // Initialize signals
        rst = 1;
        S_AXI_AWVALID = 0;
        S_AXI_WVALID = 0;
        S_AXI_BREADY = 0;
        
        // Reset for 100ns
        #10 rst = 0;
        
        // Test Case 1: Single Write Transaction
        #20;
        // Address Phase
        S_AXI_AWID = 4'h1;
        S_AXI_AWADDR = 64'h0;
        S_AXI_AWLEN = 8'h0;  // Single transfer
        S_AXI_AWSIZE = 3'h3; // 8 bytes
        S_AXI_AWBURST = 2'b01; // INCR
        S_AXI_AWVALID = 1;
        
        // Wait for AWREADY
        wait(S_AXI_AWREADY);
        @(posedge clk);
        
        // Data Phase
        S_AXI_WDATA = 64'hDEADBEEF_DEADBEEF;
        S_AXI_WSTRB = 8'hFF;
        S_AXI_WLAST = 1;
        S_AXI_WVALID = 1;
        S_AXI_BREADY = 1;
        
        // Wait for WREADY
        wait(S_AXI_WREADY);
        @(posedge clk);
        
        // Clear valid signals
        S_AXI_AWVALID = 0;
        S_AXI_WVALID = 0;
        
        // Wait for write response
        wait(S_AXI_BVALID);
        @(posedge clk);
        S_AXI_BREADY = 0;
        
        // Test Case 2: Burst Write Transaction
        #20;
        // Address Phase
        S_AXI_AWID = 4'h2;
        S_AXI_AWADDR = 64'h8;
        S_AXI_AWLEN = 8'h3;  // 4 transfers
        S_AXI_AWSIZE = 3'h3; // 8 bytes
        S_AXI_AWBURST = 2'b01; // INCR
        S_AXI_AWVALID = 1;
        
        // Wait for AWREADY
        wait(S_AXI_AWREADY);
        @(posedge clk);
        S_AXI_AWVALID = 0;
        @(negedge clk);
        
        // Burst Data Phase
        for(i = 0; i < 4; i++) begin
            S_AXI_WDATA = 64'hA5A5A5A5_0000_0000 + i;
            S_AXI_WSTRB = 8'hFF;
            S_AXI_WLAST = (i == 3);
            S_AXI_WVALID = 1;
            S_AXI_BREADY = 1;
            
            // Wait for WREADY
            wait(S_AXI_WREADY);
            // @(posedge clk);
            @(negedge clk);
        end
        
        // Clear valid signal
        S_AXI_WVALID = 0;
        
        // Wait for write response
        wait(S_AXI_BVALID);
        @(posedge clk);
        S_AXI_BREADY = 0;
        
        // Run for a while and finish
        @(negedge clk);
        instruction_next = 1;
        @(negedge clk);
        @(negedge clk);
        @(negedge clk);
        @(negedge clk);
        @(negedge clk);
        @(negedge clk);
        instruction_next = 0;
        #20
        
        $finish;
    end
    
endmodule