// Compile ahead of a matmul bench to set the accumulator mantissa width.
// A file rather than `xvlog -d ACC_MW=10`: Vivado's .bat wrappers split
// arguments on '='. Same reason mx_model_dsp.v exists.
//
// MW = 10 -> a 18-bit accumulator (S1 E7 M10).
`define ACC_MW 10
