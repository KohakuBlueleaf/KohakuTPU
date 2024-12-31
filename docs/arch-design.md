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

### Compute Unit

#### FP8-12-16 Tensor Core
In tensor core, we perform A.dot(B) + C operation in mixed precision.
Where A, B are FP8 and C is FP16. the multiplication result of A.dot(B) will be FP12. Than we use Fp12 adder tree to accumulate until we got A.dot(B).
Than this FP12 result will be filled zero in tail than add C in FP16 precision.
That is what 8-12-16 means in our tensor core.

#### FP16 ALU

In our FP16 ALU, we provide 3 input: A, B, C and 1 output
and the output will be x1 * x2 + x3
Where x1, x2, x3 are:
* x1: A, 1/A, ln(A.exp), exp(A.higher)
* x2: B, B, 1.0, exp(A.lower)
* x3: C, C, ln(A.mant), 0.0

These four different setup corresponding to:
A*B+C, B/A + C, ln(A), exp(A)
