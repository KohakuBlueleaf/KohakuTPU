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
    
    // AXI Read Address Channel
    reg [ADDR_WIDTH-1:0] S_AXI_ARADDR;
    reg [ID_WIDTH-1:0] S_AXI_ARID;
    reg [7:0] S_AXI_ARLEN;
    reg [2:0] S_AXI_ARSIZE;
    reg [1:0] S_AXI_ARBURST;
    reg S_AXI_ARVALID;
    wire S_AXI_ARREADY;
    
    // AXI Read Data Channel
    wire [ID_WIDTH-1:0] S_AXI_RID;
    wire [DATA_WIDTH-1:0] S_AXI_RDATA;
    wire [1:0] S_AXI_RRESP;
    wire S_AXI_RLAST;
    wire S_AXI_RVALID;
    reg S_AXI_RREADY;
    
    // Instruction interface
    wire [DATA_WIDTH-1:0] instruction;
    wire [$clog2(INSTRUCTION_DEPTH)-1:0] instruction_id;
    wire instruction_valid;
    reg instruction_next;
    
    // Data interface
    reg [DATA_WIDTH-1:0] data;
    reg [$clog2(INSTRUCTION_DEPTH)-1:0] data_id;
    reg data_valid;
    
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
        
        // Read Address Channel
        .S_AXI_ARADDR(S_AXI_ARADDR),
        .S_AXI_ARID(S_AXI_ARID),
        .S_AXI_ARLEN(S_AXI_ARLEN),
        .S_AXI_ARSIZE(S_AXI_ARSIZE),
        .S_AXI_ARBURST(S_AXI_ARBURST),
        .S_AXI_ARVALID(S_AXI_ARVALID),
        .S_AXI_ARREADY(S_AXI_ARREADY),
        
        // Read Data Channel
        .S_AXI_RID(S_AXI_RID),
        .S_AXI_RDATA(S_AXI_RDATA),
        .S_AXI_RRESP(S_AXI_RRESP),
        .S_AXI_RLAST(S_AXI_RLAST),
        .S_AXI_RVALID(S_AXI_RVALID),
        .S_AXI_RREADY(S_AXI_RREADY),
        
        // Instruction interface
        .instruction(instruction),
        .instruction_id(instruction_id),
        .instruction_valid(instruction_valid),
        .instruction_next(instruction_next),
        
        // Data interface
        .data(data),
        .data_id(data_id),
        .data_valid(data_valid)
    );
    
    //timeout
    initial begin
        #500;
        $display("Testbench timeout");
        $finish;
    end
    
    integer i;
    // Test stimulus
    initial begin
        // Initialize VCD dump
        $dumpfile("inst_receiver_tb.vcd");
        $dumpvars(0, InstReceiver_tb);
        
        // Initialize signals
        $display("Starting testbench...");
        rst = 1;
        S_AXI_AWVALID = 0;
        S_AXI_WVALID = 0;
        S_AXI_BREADY = 0;
        S_AXI_ARVALID = 0;
        S_AXI_RREADY = 0;
        instruction_next = 0;
        data_valid = 0;
        
        // Reset for 10ns
        #10 rst = 0;
        
        // Test Case 1: Single Write Transaction
        $display("Test Case 1: Single Write Transaction");
        #20;
        // Address Phase
        S_AXI_AWID = 4'h1;
        S_AXI_AWADDR = 64'h0;
        S_AXI_AWLEN = 8'h0;  // Single transfer
        S_AXI_AWSIZE = 3'h3; // 8 bytes
        S_AXI_AWBURST = 2'b01; // INCR
        S_AXI_AWVALID = 1;
        
        // Wait for AWREADY
        $display("Waiting for AWREADY...");
        wait(S_AXI_AWREADY);
        @(posedge clk);
        
        // Data Phase
        $display("Writing data...");
        S_AXI_WDATA = 64'hDEADBEEF_DEADBEEF;
        S_AXI_WSTRB = 8'hFF;
        S_AXI_WLAST = 1;
        S_AXI_WVALID = 1;
        S_AXI_BREADY = 1;
        
        // Wait for WREADY
        $display("Waiting for WREADY...");
        wait(S_AXI_WREADY);
        @(posedge clk);
        
        // Clear valid signals
        S_AXI_AWVALID = 0;
        S_AXI_WVALID = 0;
        
        // Wait for write response
        $display("Waiting for write response...");
        wait(S_AXI_BVALID);
        @(posedge clk);
        S_AXI_BREADY = 0;
        
        // Test Case 2: Burst Write Transaction
        $display("Test Case 2: Burst Write Transaction");
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
            @(negedge clk);
        end
        
        // Clear valid signal
        S_AXI_WVALID = 0;
        
        // Wait for write response
        wait(S_AXI_BVALID);
        @(posedge clk);
        S_AXI_BREADY = 0;
        
        // Test Case 3: Read Instructions and Send Data
        $display("Test Case 3: Read Instructions and Send Data");
        @(negedge clk);
        instruction_next = 1;
        
        // Wait for first instruction
        wait(instruction_valid);
        @(negedge clk);
        
        // Send corresponding negative data
        data = ~instruction;
        data_id = instruction_id;
        data_valid = 1;
        @(negedge clk);
        data = ~instruction;
        data_id = instruction_id;
        data_valid = 1;
        @(negedge clk);
        data = ~instruction;
        data_id = instruction_id;
        data_valid = 1;
        @(negedge clk);
        data = ~instruction;
        data_id = instruction_id;
        data_valid = 1;
        @(negedge clk);
        data = ~instruction;
        data_id = instruction_id;
        data_valid = 1;
        @(negedge clk);
        data_valid = 0;
        instruction_next = 0;
        
        // Test Case 4: Read Transaction
        $display("Test Case 4: Read Transaction");
        #20;
        // Address Phase
        S_AXI_ARID = 4'h3;
        S_AXI_ARADDR = 64'h0;
        S_AXI_ARLEN = 8'h4;  // 4 transfers
        S_AXI_ARSIZE = 3'h3; // 8 bytes
        S_AXI_ARBURST = 2'b01; // INCR
        S_AXI_ARVALID = 1;
        S_AXI_RREADY = 1;
        
        // Wait for ARREADY
        wait(S_AXI_ARREADY);
        @(posedge clk);
        S_AXI_ARVALID = 0;
        
        // Wait for read data
        wait(S_AXI_RVALID);
        @(negedge clk);
        
        // Wait for last data
        wait(S_AXI_RLAST);
        @(posedge clk);
        S_AXI_RREADY = 0;
        
        #20;
        $finish;
    end
    
endmodule