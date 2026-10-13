# multimesh v9t2 -- v9's 4+2 dies and V2 vector cores, with station 1's managers
# off block RAM, XDMA at Gen3 x4 / 128-bit as a processing accelerator, and each
# node's host interrupt on its own XDMA user vector.

set here9t2 [file dirname [file normalize [info script]]]
source [file dirname $here9t2]/v9/00_config.tcl

set design_name multimesh_v9t2
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v9t2

set SB_LPB1      820
set XDMA_LANES   X4
set XDMA_AXI_W   128
# PCI class 12/00/00: processing accelerator.
set XDMA_CLASS   {12 00 00}
set XDMA_IRQ     1
