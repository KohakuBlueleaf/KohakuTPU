## Arch Design

![HakuTPU-Overall arch](https://github.com/user-attachments/assets/d5222f88-692b-46cd-bdbf-0663eb817afc)

### hierarchical arch overview

All top level components are connected with AXI4

* XDMA (PCIE connection with host)
* DRAM (weight or result)
* Global Controller
* NoC Mesh
  * NoC Router
  * DRAM Access module
* Compute Unit
    * Compute Unit Controller (instruction decode)
    * 4 BRAM for input/output register. (3 for operands 1 for output)
    * 1 Tensor Core (4x8x4 gemm, FP8mul FP12 acc)
    * 16 ALU (FP16 FMA, with FP12 inversion, log, exp as preprocess)
    * [Possible Feature] 1 general Core/CPU
      * Should support more complex operation
        * +-*, bit-wise operation, shift, high precision
      * possibly int8/int32/FP8/FP16/FP24/FP32 inp, int8/int32/FP16/FP32 out,
      * can use up to 16/32DSP if needed. (Can build iterative division implementation in pipeline direclty)

### ALU

#### FP16 ALU

In our FP16 ALU, we provide 3 input: A, B, C and 1 output
and the output will be x1 * x2 + x3
Where x1, x2, x3 are:
* x1: A, 1/A, ln(A.exp), exp(A.higher)
* x2: B, B, 1.0, exp(A.lower)
* x3: C, C, ln(A.mant), 0.0

These four different setup corresponding to:
A*B+C, B/A + C, ln(A), exp(A)

In our general processor, we will let it to support 2 input and 1 output
To achieve complex process, we put 4 or more compute stage into this processor, and each compute stage have 4 DSP.
Which means for things like division, exp, log, sqrt, this processor can achieve one cycle throughput with iterative methods (such as newton method).
Also this processor should support different dtype. (But basically just int32 and fp32, with type conversion at input and output stage)
