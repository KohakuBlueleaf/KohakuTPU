# The per-item report after v9t6's implementation (v9t6_impl.tcl sources it
# after 80_report): v9t5/report_items.tcl's flow on this image's checkpoints,
# items against REPORT_BASE.
#   vivado -mode batch -source scripts/tcl/v9t6/report_items.tcl   (alone)
set here_ri [file dirname [file normalize [info script]]]
set tcl_ri  [file dirname $here_ri]
set cfg_ri  [file tail $here_ri]
source $here_ri/00_config.tcl
catch { close_project }
set rd_ri $proj_dir/${design_name}.runs/impl_1
# Post-route phys_opt writes its own checkpoint; it is the routed design then.
set routed_ri $rd_ri/${design_name}_wrapper_postroute_physopt.dcp
if {![file exists $routed_ri]} { set routed_ri $rd_ri/${design_name}_wrapper_routed.dcp }
foreach {tag_ri dcp_ri} [list placed $rd_ri/${design_name}_wrapper_physopt.dcp \
                              routed $routed_ri] {
    if {![file exists $dcp_ri]} { puts "@@@ report_items: no $tag_ri checkpoint ($dcp_ri)" ; continue }
    set argv [list dcp $dcp_ri tag $tag_ri margin 0 cfg $cfg_ri]
    source $tcl_ri/audit/placed_census.tcl
    # every die crossing and every clock leaving its die (audit/sll_audit.tcl)
    if {$tag_ri eq "routed"} {
        source $tcl_ri/audit/sll_audit.tcl
        puts [sll_audit $root/build/${design_name}_routed_sll_audit.txt]
    }
    close_design
    # The census leaves the config's values in place; reload them for the next.
    source $here_ri/00_config.tcl
}
set py_ri $root/.venv/Scripts/python.exe
if {![file exists $py_ri]} { set py_ri python }
set b_ri $root/build/$design_name
if {[file exists ${b_ri}_routed_classes.txt]} {
    set pl_ri [expr {[file exists ${b_ri}_placed_classes.txt] ? [list --placed ${b_ri}_placed] : {}}]
    puts [exec $py_ri $root/scripts/py/timing/triage.py ${b_ri}_routed {*}$pl_ri]
}
puts [exec $py_ri $root/scripts/py/timing/items.py $cfg_ri]
puts "@@@ report_items: ${b_ri}_items.txt"
