// Compile ahead of a matmul bench to set the accumulator mantissa width.
// A file rather than `xvlog -d ACC_MW=16`: Vivado's .bat wrappers split
// arguments on '='. Same reason mx_model_dsp.v exists.
//
// MW = 16 -> a 24-bit accumulator (S1 E7 M16).
`define ACC_MW 16
