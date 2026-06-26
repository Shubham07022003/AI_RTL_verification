module counter #(
    parameter WIDTH = 8
)(
    input wire clk,
    input wire rst,
    input wire load,
    input wire [WIDTH-1:0] data_in,
    output reg [WIDTH-1:0] count
);

    always @(posedge clk or posedge rst) begin
        if (rst) 
            count <= 8'h01; // BUG: Should reset to 0, not 1
        else if (load)
            count <= data_in;
        
        // BUG: The count increment is not mutually exclusive with load
        // This will cause count to increment even when load is high!
        count <= count + 1; 
    end

endmodule
