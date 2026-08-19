# QoR assessment + suggestions from an already-routed run. Read-only, but it
# opens the design: ~20 GB and one of the six slots while it runs.
#   vivado -mode batch -source qor_suggest.tcl -tclargs <project.xpr> <run> <outdir>

set xpr [lindex $argv 0]
set run [lindex $argv 1]
set out [lindex $argv 2]
if {$run eq ""} { set run impl_1 }
if {$out eq ""} { set out [file dirname $xpr]/qor }
file mkdir $out

set_param general.maxThreads 8
open_project $xpr
open_run $run -name qor_$run

# open_checkpoint would come up with ZERO clocks; open_run applies the IP XDC.
set nclk [llength [get_clocks -quiet]]
puts "@@@ clocks $nclk"
if {$nclk == 0} { error "no clocks -- suggestions would be judged on nothing" }

report_qor_assessment -file $out/qor_assessment.rpt
report_qor_suggestions -file $out/qor_suggestions.rpt \
                       -output_dir $out -max_paths 200
# The RQS object file feeds back into synth/impl via read_qor_suggestions +
# the runs' -rqs_file options; the .rpt beside it is the human-readable list.
write_qor_suggestions -force $out/suggestions.rqs

puts "@@@ qor done: $out/qor_assessment.rpt, qor_suggestions.rpt, suggestions.rqs"
