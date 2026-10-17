# multimesh v9t7 -- v9t6 at 4+2 a die (maps/mesh_1x2_4+2.txt): four matmul
# clusters and two V2 vector cores in the two-router mesh, one USER_CLUSTER per
# local unit as v9t6.

set here9t7 [file dirname [file normalize [info script]]]
source [file dirname $here9t7]/v9t6/00_config.tcl

set design_name multimesh_v9t7
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v9t7

set MESHES {
    0 ktpu_ship_1x2_4c2v_1m_nol2_pump
    1 ktpu_ship_1x2_4c2v_1m_nol2_pump
    2 ktpu_ship_1x2_4c2v_1m_nol2_pump
    3 ktpu_ship_1x2_4c2v_1m_nol2_pump
}

set UC_UNITS {
    u_cu0 u_cu1 u_cu2 u_cu3 u_vec0 u_vec1
    u_mag/u_pe/u_cpu u_mag/u_mag u_mag/u_pe/u_mover u_mag/u_pe/u_xform
    xache
}
set USER_CLUSTERS [list 0 $UC_UNITS 1 $UC_UNITS 2 $UC_UNITS 3 $UC_UNITS]

set REPORT_BASE v9t6
