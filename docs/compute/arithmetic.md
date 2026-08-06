## Arithmetic Operations

### FP8 mul

consider q * (k, v, m) where q,k,v,m are all FP8 floating point number (E5M2 or E4M3)
S1 E1 M1
S2 E2 M2

S1 xor S2 -> neg or pos
E1 + E2 -> new Exponent
1.M1 * 1.M2 -> new Mantissa

About mantissa, we use DSP48E2 to do the multiplication of mantissa than add the prefix 1 into it.
So it will be: `(1 + Ma) * (1 + Mb) = 1 + Ma + Mb + MaMb`

```
1.111 + 1.111 = 1 + 0.111 + 0.111 + 0.111*0.111
    0.111
*   0.111
--------------(DSP mul)
         111
        111
       111
--------------
    0.110001

    1.111
+   0.111
--------------(DSP add)
   10.110

   10.110
+   0.110001
--------------(LUT add)
   11.100001
```

#### Design1: Use 1 DSP to achieve q * [a, b, c, d]

```
000000000000001mmm                                  B(18): mantissa 1.qqq
*
000mmm00000mmm00000mmm00000mmm                      A(30): mantissa [0.aaa, 0.bbb, 0.ccc, 0.ddd]
+
000000000000000000mmm00000mmm00000mmm00000mmm000    C(48): mantissa [0.qqq, 0.qqq, 0.qqq, 0.qqq]
=
00000000000000000mmmmmmm0mmmmmmm0mmmmmmm0mmmmmmm    A*B  : 1.qqq * [0.aaa, 0.bbb, 0.ccc, 0.ddd]
+
000000000000000000mmm00000mmm00000mmm00000mmm000    C(48): mantissa [0.qqq, 0.qqq, 0.qqq, 0.qqq]
=
0000000000000000mmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm    B*A: 1.qqq * [1.aaa, 1.bbb, 1.ccc, 1.ddd]

Extra Arithmetic Operation in LUT:
Exponent Addition (5bit addition * 4)
exponent + 1 (if final mantissa > 2, 6bit addition * 4)
```

Consider E5M2, it will be

```
0000000000000001mm                                  B(18): mantissa 1.qqq
*
0000000000mm0000mm0000mm0000mm                      A(30): mantissa [0.aaa, 0.bbb, 0.ccc, 0.ddd]
+
00000000000000000000000000mm0000mm0000mm0000mm00    C(48): mantissa [0.qqq, 0.qqq, 0.qqq, 0.qqq]
=
0000000000000000000000000mmmmm0mmmmm0mmmmm0mmmmm    A*B  : 1.qqq * [0.aaa, 0.bbb, 0.ccc, 0.ddd]
+
00000000000000000000000000mm0000mm0000mm0000mm00    C(48): mantissa [0.qqq, 0.qqq, 0.qqq, 0.qqq]
=
000000000000000000000000mmmmmmmmmmmmmmmmmmmmmmmm    B*A: 1.qqq * [1.aaa, 1.bbb, 1.ccc, 1.ddd]

Extra Arithmetic Operation in LUT:
Exponent Addition (6bit addition * 4)
exponent + 1 (if final mantissa > 2, 6bit addition * 4)
```

**Note**: Use additional DSP for Exponent Additions can save LUTs (if you need)

#### Design 2: Use 2 DSP to achieve `[a, b, c].dot([q, k].T)`

FP8 E4M3

```
First DSP:
1mmm0001mmm0001mmm                                  B(18): mantissa from a, b, c
*
000mmm000000000000000000000mmm                      A(30): mantissa from q, k
=
000mmmmmmmmmmmmmmmmmmmmm000mmmmmmmmmmmmmmmmmmmmm    B*A: [1.aaa, 1.bbb, 1.ccc] x [0.qqq, 0.kkk]

Second DSP:
0s0s0s0s0s0s0000000eeee0eeee0eeee0eeee0eeee0eeee    A:B (30:18): exponent [a, b, c, , a, b, c]
+
0s0s0s0s0s0s0000000eeee0eeee0eeee0eeee0eeee0eeee    C(48): exponent [q, q, q, , k, k, k]
=
?s?s?s?s?s?s000000eeeeeeeeeeeeeeeeeeeeeeeeeeeeee    C + A:B

Extra Arithmetic Operation in LUT:
Mantissa Mul Final stage (5bit addition * 6):
    qa + 1.aaa, qb + 1.bbb, qc + 1.ccc
    ka + 1.aaa, kb + 1.bbb, kc + 1.ccc
exponent + 1 (if final mantissa > 2, 6bit addition * 6)
```

FP8 E5M2

```
First DSP:
00001mm0001mm001mm                                  B(18): mantissa from a, b, c
*
0001mm0000000000000000000001mm                      A(30): mantissa from q, k
+
000000000000000000000000000000000000000000000000    C(48)
=
00000000mmmmmmmmmmmmmmmm00000000mmmmmmmmmmmmmmmm    B*A: [1.aa, 1.bb, 1.cc] x [1.qq, 1.kk]

Second DSP:
0s0s0s0s0s0s0eeeee0eeeee0eeeee0eeeee0eeeee0eeeee    A:B (30:18): exponent [a, b, c, , a, b, c]
+
0s0s0s0s0s0s0eeeee0eeeee0eeeee0eeeee0eeeee0eeeee    C(48): exponent [q, q, q, , k, k, k]
=
?s?s?s?s?s?seeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee    C + A:B

Extra Arithmetic Operation in LUT:
exponent + 1 (if final mantissa > 2, 6bit addition * 6)
```

### FP16 Accumulation

The addition between floating point number is basically shift + integer addition.

Which means for FP16 it will be addition between 5bit integer (for how many bit shift) than do 10bit integer add (for mantissa).

Therefore, we can put the exponent addition in LUT than use DSP for 10bit addition.
Which means we can do [a, b, c, d] + [q, k, v, m] = [a+q, b+k, c+v, d+m] with only 1 DSP.
(DSP48 support 48bit addition within one cycle, and 10bit + 10bit = 11bit)

No much trick... only brute force.

### FP16 FMA

```
FMA(a, b, c) = a*b + c
First DSP:
010000000100000001                                  B(18)
*
(
0000000000000000000000000eeeee                      A(30): Exponent from a
+
0000000000000110001000eeeee                         D(27): Exponent from b and exponent offset(-15 in 6bit)
)
+
0000000000000000000000000000000000eeeeee00000000    C(48): Exponent from -c (6bit) 
=
0000000000000000001100010xeeeeee0xeeeeee00eeeeee    B*(A+D): Exponent a+b-15 and a+b 
+
0000000000000000000000000000000000eeeeee00000000    C(48): Exponent from -c (6bit) 
=
0000000000000000001100010xeeeeeexxeeeeee00eeeeee    B*(A+D) + C Exponent a+b-15(ab_exp), and a+b-15-c (shift of c_mant)


Second DSP:
(if shift left larger than 11, directly take c as result)
00000001.mmmmmmmmmm                                  B(18): mantissa from a
*
00000000000000000001.mmmmmmmmmm                      A(30): mantissa from b
+
0000000000000000000000000001.mmmmmmmmmm0000000000    C(48): shifted mantissa from c 
=
0000000000000000000000000mmm.mmmmmmmmmmmmmmmmmmmm    B*A + c: 1.a * 1.b +/- 1.c(shifted)

```

### Division

For FP16 division a/b, we decide to use FP12 inverse: inv(a) = 1/a with FP16 FMA unit to achieve it.

In division, we have `x = 2^e * 1.mmmmmm, 1/x = 2^-e * 1/1.mmmmmm`. Since the inverse of 1.mmmmmm is between 0.5-1.0 in decimal, which means it will be 0.1xxxx or 1.0 as result.. Basically the result of 1/1.mmmmmm is a 6bit to 11bit function and we can direclty hardcoded 11 LUT for this logic.

Therefore, we know the result will be: `new_e = -e-(m!=0), new_m = 1.xxxxx`, where xxxxx is the LUT result.

For subnormal number, we have `x = 0.mmmmmm`, which can be seen as 6 to 11(sign is original x sign) function, so need extra 11 LUT for it.

and for exp process, it will be -exp+29+result_mant[10] (to see if 1/1.xxxxx = 1.0). which is actulaly 6input 5output (5bit exp + 1bit mant[10], output 5bit exp). So it need other 5 LUT.

totally we need at least 11 + 11 + 5 = 27 LUT for inverse mapping. The real LUT cost here is around 30.

### Log

For log (specifically, log_e() here), we have `log(x) = e * log(2) + log(1.mmmmmm)`. Where e*log(2) and log(1.mmmmmm) are both FP16.

We know log(1.mmmmmm) is positive so it is 6input 15output function, which need 15 LUT6, and e*log(2) is 5input 16output function, since each LUT6 in xilinx7 series is actually 2 LUT5 with mux, we can only use 8LUT6 to achieve 5input 16 output application.

for subnormal number, we have `log(x) = log(0.mmmmmm)`, which need another 15LUT (we know it will be negative, and NaN for 0).

totally we need 15 + 8 + 15 = 38 LUT for log mapping

### EXP

For exponential `e^x`, we consider a fixed point number representation of x here which can be seen as (1.mmmmmm) << (2^exp - 15).
Than we consider 3 chunk, 5 bit in integer, 5 bit in higher 5bit in fraction and 5bit in lower 5bit of fraction part. 

We only need 5bit in integer since exp(15) will be inf and exp(-15) will be 0.

Than we consider 2 sitaution: integer is 0 -> use 2 fraction part, integer is not 0 -> use integer and higher fraction part.

So we need 3 6bit(5bit + sign bit) to 15bit output (can be optimized to 15*2+11), and we use 2 of them as partial result tham multiply together (exp(a+b) = exp(a) * exp(b))

### FP24 FMA

Similar to FP16 but we need different arrangement for the exponent part, since FP24 have 7 exponent bit:

```
DSP for exponent:
000001000000000001                                  B(18)
*
(
00000000000000000000000eeeeeee                      A(30): Exponent from a
+
000000ooooooooo00000eeeeeee                         D(27): Exponent from b and exponent offset
)
+
0000000000000000000000000000eeeeeeee0000oooooooo    C(48): Exponent from -c
=
000000000000000ooooooooo000xeeeeeeee0000eeeeeeee    B*(A+D): Exponent a+b-63 and a+b 
+
0000000000000000000000000000eeeeeeee0000oooooooo    C(48): Exponent from -c
=
000000000000000ooooooooo00xxeeeeeeee0000eeeeeeee    B*(A+D) + C Exponent a+b-63(ab_exp), and a+b-63-c (shift of c_mant)

DSP for mantissa:
0001.mmmmmmmmmmmmmm                                  B(18): mantissa from a (cutoff last 2 mantissa bit)
*
0000000000000001.mmmmmmmmmmmmmm                      A(30): mantissa from b (cutoff last 2 mantissa bit)
+
00000000000000000001.mmmmmmmmmmmmmmmm000000000000    C(48): shifted mantissa from c 
=
00000000000000000mmm.mmmmmmmmmmmmmmmmmmmmmmmmmmmm    B*A + c: 1.a * 1.b +/- 1.c(shifted)
```
