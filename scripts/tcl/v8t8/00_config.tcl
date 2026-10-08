# multimesh v8t8 -- the compute image: v8t7's node, Xache, station bus and
# interlink, each die's node inside a generated 2x2 mesh (7+2 on dies 0/2/3,
# 5+2 on die 1), die-wide pblocks, clocks built at the v8t3 rates.

set here8 [file dirname [file normalize [info script]]]
source [file dirname $here8]/v8t7/00_config.tcl

set design_name multimesh_v8t8
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v8t8

set MESHES {
    0 ktpu_ship_2x2_7c2v_1m_nol2_pump
    1 ktpu_ship_2x2_5c2v_1m_nol2_pump
    2 ktpu_ship_2x2_7c2v_1m_nol2_pump
    3 ktpu_ship_2x2_7c2v_1m_nol2_pump
}
set CMP_COLS    {}

set OOC_JOBS    4
