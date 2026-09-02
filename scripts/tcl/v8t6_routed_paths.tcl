# v8t6 ROUTED path census, read-only: v8t3/87_synth_paths.tcl on the routed
# checkpoint, so it runs beside the project without opening it; every merged
# class set against the v8t5 routed census (`-tclargs base FILE` for another).
#   vivado -mode batch -source scripts/tcl/v8t6_routed_paths.tcl -tclargs margin 0.5
set here [file dirname [file normalize [info script]]]
source $here/v8t6/00_config.tcl
set V8_DCP $proj_dir/${design_name}.runs/impl_1/${design_name}_wrapper_routed.dcp
if {![file exists $V8_DCP]} { error "no routed checkpoint at $V8_DCP" }
if {![info exists V8_BASE]} { set V8_BASE $root/build/multimesh_v8t5_routed_classes_merged.txt }
source $here/v8t3/87_synth_paths.tcl
