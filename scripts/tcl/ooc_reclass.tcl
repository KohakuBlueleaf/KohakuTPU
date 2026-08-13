# Re-classifies checkpoints ALREADY on disk, so the compute/control split costs
# a checkpoint open instead of a re-synthesis. OOC_DCPS is a space-separated list.

set root C:/Users/apoll/Desktop/code/Project/KohakuTPU
source $root/scripts/tcl/ooc_class.tcl

foreach d $::env(OOC_DCPS) {
    open_checkpoint $root/build/ooc/$d
    set wns [get_property SLACK [lindex [get_timing_paths -max_paths 1 -setup] 0]]
    puts [format "@@@ === %s   WNS %.3f ns" $d $wns]
    ooc_classify
    close_design
}
puts "@@@ DONE"
