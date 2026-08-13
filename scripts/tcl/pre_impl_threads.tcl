# TCL.PRE hook for impl steps: Vivado defaults to 2 threads, cap is 32. Set on
# every step because re-running one step starts a fresh process.

set_param general.maxThreads 32
puts "@@@ general.maxThreads = [get_param general.maxThreads]"
