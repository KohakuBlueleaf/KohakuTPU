# multimesh v8t5 -- v8t4's shape exactly (four system nodes, no mesh), the
# Xache at tier T4 and the station bus at tier S2. No topology, width or
# clocking changes.
#
# T4: every Xache FIFO class on inferred LUTRAM, the reorder ring 64 deep on
# the 16-beat read slot (592 LUT a master against 2,887 at a page); the slot
# bounds every read burst a node may issue, so the same value sets each
# node's DRAM port AR split. OOC: 23,055 LUT, 0 tiles, 358 MHz (v8t4's Xache
# was 17,659 / 184 / 260.7 with the chain live).
# S2: every depth-16 station queue on LUTRAM (threshold 120) and the xdma
# manager's queues at their 64 / 128 floors with station 1 at 270, so they
# convert too; 6 tiles (jtag) stay. OOC: 26,730 LUT, 330 MHz (v8t4's station
# 26,473 / 90.5 tiles / 300).

set here5 [file dirname [file normalize [info script]]]
source [file dirname $here5]/v8t4/00_config.tcl

set design_name multimesh_v8t5
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v8t5

set KX_RB_BEATS  16
set KX_MEM_TRUNK distributed
set KX_MEM_RB    distributed
set KX_MEM_HRD   distributed
set KX_MEM_HWR   distributed
set LUT_PER_BRAM 120
set SB_LPB1      270
set SB_MREQ1     64
set SB_MRSP1     128
