# What this Vivado will actually accept for general.maxThreads, and whether
# route_design advertises -ultrathreads.

puts "@@@ default maxThreads = [get_param general.maxThreads]"
foreach n {8 16 32 64} {
    if {[catch {set_param general.maxThreads $n} e]} {
        puts "@@@ set $n REJECTED"
    } else {
        puts "@@@ set $n -> [get_param general.maxThreads]"
    }
}
set h [help route_design]
puts "@@@ ultrathreads in route_design help: [string match {*ultrathreads*} $h]"
puts "@@@ DONE"
