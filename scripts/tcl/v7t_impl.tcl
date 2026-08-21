# v7t: impl + bitstream from the completed synth. Deliberately does NOT go
# through multimesh_v6_bd.tcl -- its preamble deletes the sb_bd_line4 mref
# cache and forces a repackage on open, which is the crash zone that killed
# the 03:31 run mid-packaging with no error.
open_project C:/Users/apoll/Desktop/vivado/multimesh_v7t/multimesh_v7t.xpr

if {[get_property top [current_fileset]] ne "multimesh_v7t_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is [get_property PROGRESS [get_runs synth_1]], not complete"
}

launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "impl failed: see multimesh_v7t.runs/impl_1/runme.log"
}
puts "@@@ v7t bitstream done"
