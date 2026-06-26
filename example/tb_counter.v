
`timescale 1ns/1ps

module tb_counter;
    parameter WIDTH = 8;
    reg clk, rst, load;
    reg [WIDTH-1:0] data_in;
    wire [WIDTH-1:0] count;

    counter #(.WIDTH(WIDTH)) dut (.*);

    initial begin
        clk = 0;
        forever #5 clk = ~clk;
    end

    initial begin
        rst = 1; load = 0; data_in = 0;
        #20 rst = 0;
        
        // Test load
        #10 load = 1; data_in = 8'hAA;
        #10 load = 0;
        
        #50 $finish;
    end
endmodule
