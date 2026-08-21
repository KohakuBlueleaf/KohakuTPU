# v7: impl through write_bitstream from completed synth. Bypasses the
# multimesh_v6_bd.tcl preamble (its mref-cache deletion forces a repackage on
# open -- the silent-crash zone that killed the v7t 03:31 run).
open_project C:/Users/apoll/Desktop/vivado/multimesh_v7/multimesh_v7.xpr

if {[get_property top [current_fileset]] ne "multimesh_v7_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is [get_property PROGRESS [get_runs synth_1]], not complete"
}

launch_runs impl_1 -to_step write_bitstream -jobs 4
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "impl failed: see multimesh_v7.runs/impl_1/runme.log"
}
puts "@@@ v7 bitstream done"
