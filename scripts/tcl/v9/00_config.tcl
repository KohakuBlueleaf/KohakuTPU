# multimesh v9 -- v8t9's node, Xache, station bus, interlink and clocks; every
# SLR a 1x2 mesh of 4 clusters + 2 V2 vector cores (maps/mesh_1x2_4+2.txt).

set here9v [file dirname [file normalize [info script]]]
source [file dirname $here9v]/v8t9/00_config.tcl

set design_name multimesh_v9
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v9

set MESHES {
    0 ktpu_ship_1x2_4c2v_1m_nol2_pump
    1 ktpu_ship_1x2_4c2v_1m_nol2_pump
    2 ktpu_ship_1x2_4c2v_1m_nol2_pump
    3 ktpu_ship_1x2_4c2v_1m_nol2_pump
}
set VEC_CORE    v2
