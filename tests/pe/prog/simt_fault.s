# The region fault, which nothing else exercises.
#
# A per-lane access whose address decodes to no region must FAULT, not issue.
# That check is REGISTERED and sticky across the LSU walk -- it was moved off
# the combinational path because the 32-bit address adder plus the region decode
# sat between the instruction window and the halt FSM, and was the assembled
# PE's binding path. A path that moved is a path that needs a test.
#
# The witness is the halt CAUSE, not the memory: cause 3 (fault), not 1 (ecall),
# and the `ecall` below must never be reached.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_fault.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0
        saddi   s3, s0, 3
        sslli   s3, s3, 28      # 0x30000000 -- region 3 is mapped to nothing

        vlaneid x5
        addi    x6, x5, 1

        vsinw2  x6, s3          # must FAULT here

        addi    x10, x0, 0x55   # never reached
        ecall
