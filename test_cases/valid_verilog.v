// valid_verilog.v - a correct design: 4-to-1 mux (combinational) + counter (sequential).
// Expected: compiles cleanly, no latch, no multiple drivers.
module valid_design (
    input        clk,
    input        rst,
    input  [1:0] sel,
    input  [3:0] a, b, c, d,
    output reg [3:0] y,
    output reg [3:0] count
);
    // Combinational logic: every branch assigns y, and there is a default.
    always @(*) begin
        case (sel)
            2'b00:   y = a;
            2'b01:   y = b;
            2'b10:   y = c;
            default: y = d;
        endcase
    end

    // Sequential logic: a flip-flop based counter using non-blocking assignments.
    always @(posedge clk) begin
        if (rst)
            count <= 4'd0;
        else
            count <= count + 4'd1;
    end
endmodule
