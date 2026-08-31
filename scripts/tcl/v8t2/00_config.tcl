# multimesh_v8t2 = multimesh_v8t with the banked Kohaku Xache array: every knob
# is v8t's except the three below. The stages are v8t's files, run with this
# config loaded first (multimesh_v8t2_bd.tcl, v8t2_impl.tcl, 75_verify_bd.tcl).
source [file dirname [file normalize [info script]]]/../v8t/00_config.tcl
set design_name multimesh_v8t2
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v8t2
# eight single-URAM-deep banks per home (kx_carray BANKS); v8t is 1
set KX_BANKS    8
