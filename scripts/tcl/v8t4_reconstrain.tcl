# Regenerate v8t4's pblock and clock XDC against the built project, without
# rebuilding the block design. The files are already in constrs_1, so the next
# synthesis and implementation read the new ones.
#   vivado -mode batch -source scripts/tcl/v8t4_reconstrain.tcl
set here [file dirname [file normalize [info script]]]
source $here/v8t4/00_config.tcl
open_project $proj_dir/${design_name}.xpr
source $here/v8t3/60_constraints.tcl
puts "@@@ $design_name constraints regenerated"
