## Resource cost and throughput and latency

In this documentation we list all the vivado synthesised result to check the resource cost.

* FP8VectorMul:

  * Input:
    * Design1: q, a, b, c, d (FP8 E5M2/E4M3)
    * Design2: q, k, a, b, c (FP8 E5M2/E4M3)
  * output:
    * Design1: qa, qb, qc, qd (FP12)
    * Design2: qa, qb, qc, ka, kb, kc (FP12)
  * resource cost:
    * Design 1: 123 LUT, 118 FF, 1 DSP
    * Design 2: 163 LUT, 108 FF, 2 DSP
  * performance:
    * 3-cycle latency, 1-cycle throughput
* FPVectorAdd (can customize Exponent and Mantissa)

  * Input: a1, b1, c1, d1, a2, b2, c2, d2
  * Output: a1+a2, b1+b2, c1+c2, d1+d2: shi
  * resource cost:
    * **Following numbers are from implementation run**
    * E5M6 (FP12) : 333 LUT, 119 FF, 1 DSP
    * E5M10 (FP16) : 546 LUT, 148 FF, 1 DSP
  * performance:
    * 2-cycle latency, 1-cycle throughput
* FP12Inversion

  * Input: a
  * Output: 1/a
  * resource cost: 38LUT
  * performance: combinational

* FP8-FP12 4x8x4 Tensor Core
  * Input: A(4x8 FP8), B(8x4 FP8), C(4x4 FP16)
  * Output: (AxB + C) (4x4 FP16)
  * resource cost: 12731LUT, 7549FF, 64DSP
  * performance:
    * 16-cycle latency, 1-cycle throughput

* FP16ALU array
  * Input: [a, b, c]*16 (FP16), 6bit OPMODE
  * Output: [out] * 16 (FP16)
  * resource cost: 6643LUT, 2090FF, 32DSP
  * performance:
    * 4-cycle latency, 1-cycle throughput