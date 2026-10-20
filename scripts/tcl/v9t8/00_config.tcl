# multimesh v9t8 -- v9t7 (4+2 a die) with each die split into the solver's two
# soft partitions, system and compute; compute borrows two system regions' DSP.

set here9t8 [file dirname [file normalize [info script]]]
source [file dirname $here9t8]/v9t7/00_config.tcl

set design_name multimesh_v9t8
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v9t8

set SN_BOX       soft
set CMP_PLACE    box
set SN_UNITS     {0 {}  1 {}  2 {}  3 {}}
set SN_CMP_UNITS {
    0 {u_cu0 u_cu1 u_cu2 u_cu3 u_vec0 u_vec1}
    1 {u_cu0 u_cu1 u_cu2 u_cu3 u_vec0 u_vec1}
    2 {u_cu0 u_cu1 u_cu2 u_cu3 u_vec0 u_vec1}
    3 {u_cu0 u_cu1 u_cu2 u_cu3 u_vec0 u_vec1}
}
set USER_CLUSTERS {}
# Soft die boxes, as v9t6 and v9t7 placed (their IS_SOFT preceded CONTAIN_ROUTING,
# which resets it): 0 of 2,079,259 v9t7 mesh cells left their die.
set DIE_PB_SOFT 1

# Solver output (scratchpad fp/v9t8_shapes.js, v9t8 synthesis unit sizes, die 1
# keeping XDMA's X6/X7 row-0 regions): system LUT 37 % (48 % die 1), URAM 81 %;
# compute LUT 56 %, RAMB36 63 %, DSP 72 % at 70 / 85 / 90 / 85 % limits.
set SN_REGIONS {
    0 {X4Y1 X4Y2 X4Y3 X6Y0 X7Y0 X5Y1 X6Y1 X7Y1 X5Y2 X6Y2 X7Y2 X5Y3 X6Y3 X7Y3}
    1 {X4Y5 X4Y6 X4Y7 X6Y4 X7Y4 X5Y5 X6Y5 X7Y5 X5Y6 X6Y6 X7Y6 X5Y7 X6Y7 X7Y7}
    2 {X4Y9 X4Y10 X4Y11 X6Y8 X7Y8 X5Y9 X6Y9 X7Y9 X5Y10 X6Y10 X7Y10 X5Y11 X6Y11 X7Y11}
    3 {X4Y12 X4Y13 X4Y14 X5Y12 X6Y12 X7Y12 X5Y13 X6Y13 X7Y13 X5Y14 X6Y14 X7Y14 X6Y15 X7Y15}
}
set SN_CMP_REGIONS {
    0 {X0Y0 X1Y0 X2Y0 X3Y0 X4Y0 X5Y0 X0Y1 X1Y1 X2Y1 X3Y1 X0Y2 X1Y2 X2Y2 X3Y2 X0Y3 X1Y3 X2Y3 X3Y3}
    1 {X0Y4 X1Y4 X2Y4 X3Y4 X4Y4 X5Y4 X0Y5 X1Y5 X2Y5 X3Y5 X0Y6 X1Y6 X2Y6 X3Y6 X0Y7 X1Y7 X2Y7 X3Y7}
    2 {X0Y8 X1Y8 X2Y8 X3Y8 X4Y8 X5Y8 X0Y9 X1Y9 X2Y9 X3Y9 X0Y10 X1Y10 X2Y10 X3Y10 X0Y11 X1Y11 X2Y11 X3Y11}
    3 {X0Y12 X1Y12 X2Y12 X3Y12 X0Y13 X1Y13 X2Y13 X3Y13 X0Y14 X1Y14 X2Y14 X3Y14 X0Y15 X1Y15 X2Y15 X3Y15 X4Y15 X5Y15}
}
set SN_DSP_LEND {
    0 {X6Y0 X5Y1}
    1 {X6Y4 X5Y5}
    2 {X6Y8 X5Y9}
    3 {X5Y14 X6Y15}
}

set REPORT_BASE v9t7
