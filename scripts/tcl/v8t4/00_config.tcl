# multimesh v8t4 -- v8t3's shape with ONE knob turned: every die drives its own
# sysnode clock and its own bus clock, so no clock net spans the four dies.
# Every stage is v8t3's, so the two builds differ by nothing else.
#
# What that turns into: the Xache's boundary trunks become clock crossings
# (KX_PCLK), so does each interlink hop (IL_ASYNC) and each station link
# (LINK_CDC). A node still meets its Xache partition and its station's port-0
# with no crossing at all -- they share die i's clock.

set here4 [file dirname [file normalize [info script]]]
source [file dirname $here4]/v8t3/00_config.tcl

set design_name multimesh_v8t4
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v8t4

set PER_DIE_CLK 1
set KX_PCLK     1
set IL_ASYNC    1
set LINK_CDC    1
