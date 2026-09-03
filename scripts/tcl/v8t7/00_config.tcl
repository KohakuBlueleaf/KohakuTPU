# multimesh v8t7 -- v8t6's RTL, tiers and knobs, placed: three register stages
# in each pipe half (IL_STAGES) and a compute-half pblock per die (CMP_COLS) so
# no node straddles its die's DDR4 column (clock-region column X4).
#
# Sizing (build/xcvu13p_regions.txt): a west half X0-X3 holds 217k LUT, 384
# RAMB36, 128 URAM; an east half X5-X7 161k LUT, 192 RAMB36, 128 URAM. A node
# needs 24k LUT, 66 RAMB36, 65 URAM; its Xache home 64 URAM more, which the
# 128 cannot hold with the node, so the homes stay die-wide beside their MIG
# (CMP_HOME empty). Die 0's node sits west in v8t6, dies 1-3 east; die 1's
# east half also carries the XDMA (53k LUT) and the 10k-LUT station 1.

set here7 [file dirname [file normalize [info script]]]
source [file dirname $here7]/v8t6/00_config.tcl

set design_name multimesh_v8t7
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v8t7

set IL_STAGES   3
set CMP_COLS    {0 {X0 X3}  1 {X5 X7}  2 {X5 X7}  3 {X5 X7}}
set CMP_STN     {0 1 2 3}
set CMP_HOME    {}
