# v7.1: impl through write_bitstream from completed synth. Bypasses the
# multimesh_v6_bd.tcl preamble, whose mref-cache deletion forces a repackage.
open_project C:/Users/apoll/Desktop/vivado/multimesh_v71/multimesh_v71.xpr

if {[get_property top [current_fileset]] ne "multimesh_v71_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is [get_property PROGRESS [get_runs synth_1]], not complete"
}

launch_runs impl_1 -to_step write_bitstream -jobs 4
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "implementation failed: multimesh_v71.runs/impl_1/runme.log"
}
puts "@@@ v71 bitstream done"
