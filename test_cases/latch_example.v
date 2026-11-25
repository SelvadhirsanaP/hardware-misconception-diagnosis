// latch_example.v - unintended latch.
// 'q' is only assigned when 'en' is 1. When en = 0 the hardware must remember q,
// so synthesis builds a latch. The student probably wanted a plain combinational circuit.
module latch_example (input en, input d, output reg q);
    always @(*) begin
        if (en)
            q = d;
    end
endmodule
