# Regenerate a built project's XDC (already in constrs_1) without the block design.
#   vivado -mode batch -source scripts/tcl/reconstrain.tcl -tclargs <v9t8>
set here [file dirname [file normalize [info script]]]
lassign $argv ver
if {![file exists $here/$ver/00_config.tcl]} { error "no config: $here/$ver/00_config.tcl" }
source $here/$ver/00_config.tcl
open_project $proj_dir/${design_name}.xpr
source $here/v8t3/60_constraints.tcl
puts "@@@ $design_name constraints regenerated"
