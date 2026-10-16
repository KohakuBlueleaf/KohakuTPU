# multimesh v9t4 -- v9t3 at 2+2 a die (maps/mesh_1x2_2+2.txt), with the matmul
# clocks as one BUFGCE_DIV pair, each die split at its MIG's I/O column and the
# MIGs held to the right of it.

set here9t4 [file dirname [file normalize [info script]]]
source [file dirname $here9t4]/v9t3/00_config.tcl

set design_name multimesh_v9t4
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v9t4

set MESHES {
    0 ktpu_ship_1x2_2c2v_1m_nol2_pump
    1 ktpu_ship_1x2_2c2v_1m_nol2_pump
    2 ktpu_ship_1x2_2c2v_1m_nol2_pump
    3 ktpu_ship_1x2_2c2v_1m_nol2_pump
}

set MAT_CLK_PAIR  1
set MIG_FLOORPLAN FULL

# Every MIG's I/O column is in clock-region column X4 and FULL puts its
# controller to the right, so the node, its Xache partition and station take
# X4-X7 beside it on every die (die 1 with XDMA). Per half, v9t3's synthesis:
# node ~42k LUT/74 RAMB36/65 URAM, Xache partition ~9k/64 URAM, station ~5k
# (16.5k on die 1), cluster ~21k/34 RAMB36/9 URAM/320 DSP, V2.1 core ~41k/89
# RAMB36/76 DSP. A core beside the node would put X4-X7 at ~81% of its block
# RAM, so both cores go with the clusters in X0-X3: 124k LUT of ~217k, 246 of
# 384 RAMB36, 792 of 1,536 DSP.
set SN_BOX       soft
set SN_COLS      {0 {X4 X7}  1 {X4 X7}  2 {X4 X7}  3 {X4 X7}}
set SN_CMP_COLS  {0 {X0 X3}  1 {X0 X3}  2 {X0 X3}  3 {X0 X3}}
set SN_UNITS     {0 {}  1 {}  2 {}  3 {}}
set SN_CMP_UNITS {
    0 {u_cu0 u_cu1 u_vec0 u_vec1}
    1 {u_cu0 u_cu1 u_vec0 u_vec1}
    2 {u_cu0 u_cu1 u_vec0 u_vec1}
    3 {u_cu0 u_cu1 u_vec0 u_vec1}
}
set SN_PIPES     0
