// multiple_driver_example.v - two different sources drive the same wire 'y'.
// In real hardware two outputs fighting over one wire cause a short / unknown value.
module multi_driver (input a, input b, output y);
    assign y = a;
    assign y = b;
endmodule
