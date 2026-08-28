# Dump all CONFIG.* props of an IP so we know the exact knob names. No synth.
#   -tclargs <ipglob>
set part xcvu13p-fhgb2104-2L-e
set glob [lindex $argv 0]
if {$glob eq ""} { set glob *:system_cache:* }
create_project -force -in_memory -part $part
set ipdef [lindex [get_ipdefs -all $glob] end]
puts "@@@ ipdef=$ipdef"
create_ip -vlnv $ipdef -module_name probeip
foreach p [lsort [list_property [get_ips probeip]]] {
    if {[string match CONFIG.* $p]} {
        puts "PROP $p = [get_property $p [get_ips probeip]]"
    }
}
puts "@@@ props done"
