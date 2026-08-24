/* GENERATED from tests/pe/tools/rv_simd_isa.py -- DO NOT EDIT.
 * Regenerate with `python tests/pe/tools/rv_simd_emit.py`; the ISA test
 * regenerates and compares, so a hand edit here fails rather than
 * quietly disagreeing with the assembler and the golden model. */

/* Every intrinsic is `volatile`, and that is not caution.
 *
 * Vector register numbers are IMMEDIATES here, not operands the
 * compiler allocates -- which is what buys `no compiler fork`. The
 * consequence is that GCC cannot see the vector state at all: two
 * identical vdot calls are not one value, they accumulate. So the
 * compiler may not reorder, hoist or common these, and it cannot
 * software-pipeline the vector datapath. On an in-order single-issue
 * core whose multi-cycle ops stall in the existing hazard unit that
 * costs little; it is the honest price of the no-fork path.
 */
#ifndef KHS_INTRIN_H
#define KHS_INTRIN_H

#include <stdint.h>

#define KHS_VSPAD_BASE 0x40000000u

/* vd <- the vector at rs1+imm. Line-aligned by contract: an address that is not a multiple of the vector width faults. */
#define khs_vld(vd, p, imm) \
    __asm__ volatile(".insn i 0x0b, 0, x" #vd ", %0, " #imm : : "r"(p) : "memory")

/* the vector at rs1+imm <- vs. Line-aligned; a misaligned address faults. */
#define khs_vst(vs, p, imm) \
    __asm__ volatile(".insn i 0x0b, 1, x" #vs ", %0, " #imm : : "r"(p) : "memory")

/* element-wise wrapping add (s8 elements) */
#define khs_vadd_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x00, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise wrapping add (s16 elements) */
#define khs_vadd_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x01, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise wrapping add (s32 elements) */
#define khs_vadd_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x02, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise wrapping subtract (s8 elements) */
#define khs_vsub_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x04, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise wrapping subtract (s16 elements) */
#define khs_vsub_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x05, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise wrapping subtract (s32 elements) */
#define khs_vsub_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x06, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating add (s8 elements) */
#define khs_vsadd_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x08, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating add (s16 elements) */
#define khs_vsadd_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x09, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating add (s32 elements) */
#define khs_vsadd_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x0a, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating subtract (s8 elements) */
#define khs_vssub_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x0c, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating subtract (s16 elements) */
#define khs_vssub_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x0d, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating subtract (s32 elements) */
#define khs_vssub_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x0e, x" #vd ", x" #vs1 ", x" #vs2)

/* signed minimum (s8 elements) */
#define khs_vmin_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x10, x" #vd ", x" #vs1 ", x" #vs2)

/* signed minimum (s16 elements) */
#define khs_vmin_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x11, x" #vd ", x" #vs1 ", x" #vs2)

/* signed minimum (s32 elements) */
#define khs_vmin_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x12, x" #vd ", x" #vs1 ", x" #vs2)

/* signed maximum (s8 elements) */
#define khs_vmax_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x14, x" #vd ", x" #vs1 ", x" #vs2)

/* signed maximum (s16 elements) */
#define khs_vmax_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x15, x" #vd ", x" #vs1 ", x" #vs2)

/* signed maximum (s32 elements) */
#define khs_vmax_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x16, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise product, low half kept (s8 elements) */
#define khs_vmul_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x18, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise product, low half kept (s16 elements) */
#define khs_vmul_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x19, x" #vd ", x" #vs1 ", x" #vs2)

/* bitwise and */
#define khs_vand(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 3, 0x00, x" #vd ", x" #vs1 ", x" #vs2)

/* bitwise or */
#define khs_vor(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 3, 0x01, x" #vd ", x" #vs1 ", x" #vs2)

/* bitwise exclusive or */
#define khs_vxor(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 3, 0x02, x" #vd ", x" #vs1 ", x" #vs2)

/* vs1 & ~vs2 */
#define khs_vandn(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 3, 0x03, x" #vd ", x" #vs1 ", x" #vs2)

/* shift left logical (s8 elements) */
#define khs_vslli_s8(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x00, x" #vd ", x" #vs1 ", x" #sh)

/* shift left logical (s16 elements) */
#define khs_vslli_s16(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x01, x" #vd ", x" #vs1 ", x" #sh)

/* shift left logical (s32 elements) */
#define khs_vslli_s32(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x02, x" #vd ", x" #vs1 ", x" #sh)

/* shift right logical (s8 elements) */
#define khs_vsrli_s8(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x04, x" #vd ", x" #vs1 ", x" #sh)

/* shift right logical (s16 elements) */
#define khs_vsrli_s16(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x05, x" #vd ", x" #vs1 ", x" #sh)

/* shift right logical (s32 elements) */
#define khs_vsrli_s32(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x06, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic (s8 elements) */
#define khs_vsrai_s8(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x08, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic (s16 elements) */
#define khs_vsrai_s16(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x09, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic (s32 elements) */
#define khs_vsrai_s32(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x0a, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic, ROUNDING (add half an ulp first) -- the requantise primitive, and the one a plain vsrai gets subtly wrong (s8 elements) */
#define khs_vsrari_s8(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x0c, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic, ROUNDING (add half an ulp first) -- the requantise primitive, and the one a plain vsrai gets subtly wrong (s16 elements) */
#define khs_vsrari_s16(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x0d, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic, ROUNDING (add half an ulp first) -- the requantise primitive, and the one a plain vsrai gets subtly wrong (s32 elements) */
#define khs_vsrari_s32(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x0e, x" #vd ", x" #vs1 ", x" #sh)

/* acc[ad] += the dot product of the elements within each 32-bit lane (s8 elements) */
#define khs_vdot_s8(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 5, 0x00, x" #ad ", x" #vs1 ", x" #vs2)

/* acc[ad] += the dot product of the elements within each 32-bit lane (s16 elements) */
#define khs_vdot_s16(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 5, 0x01, x" #ad ", x" #vs1 ", x" #vs2)

/* acc[ad] -= the dot product of the elements within each 32-bit lane (s8 elements) */
#define khs_vdotn_s8(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 5, 0x04, x" #ad ", x" #vs1 ", x" #vs2)

/* acc[ad] -= the dot product of the elements within each 32-bit lane (s16 elements) */
#define khs_vdotn_s16(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 5, 0x05, x" #ad ", x" #vs1 ", x" #vs2)

/* acc[ad] <- 0 */
#define khs_vaccz(ad) \
    __asm__ volatile(".insn r 0x0b, 5, 0x08, x" #ad ", x0, x0")

/* vd <- acc[as1], as int32 lanes */
#define khs_vaccrd(vd, as1) \
    __asm__ volatile(".insn r 0x0b, 5, 0x0c, x" #vd ", x" #as1 ", x0")

/* acc[ad] <- vs1, as int32 lanes -- how a bias vector seeds an accumulation */
#define khs_vaccwr(ad, vs1) \
    __asm__ volatile(".insn r 0x0b, 5, 0x10, x" #ad ", x" #vs1 ", x0")

/* every 32-bit lane of vd <- xs1 */
#define khs_vsplat(vd, xs1) \
    __asm__ volatile(".insn r 0x0b, 6, 0x00, x" #vd ", %0, x0" : : "r"(xs1))

/* xd <- 32-bit lane `sh` of vs1 */
#define khs_vextr(vs1, sh) ({ \
    int32_t _khs_r; \
    __asm__ volatile(".insn r 0x0b, 6, 0x01, %0, x" #vs1 ", x" #sh : "=r"(_khs_r)); \
    _khs_r; })

/* xd <- the sum of vs1's 32-bit lanes */
#define khs_vredsum(vs1) ({ \
    int32_t _khs_r; \
    __asm__ volatile(".insn r 0x0b, 6, 0x02, %0, x" #vs1 ", x0" : "=r"(_khs_r)); \
    _khs_r; })

/* xd <- the signed max of vs1's 32-bit lanes */
#define khs_vredmax(vs1) ({ \
    int32_t _khs_r; \
    __asm__ volatile(".insn r 0x0b, 6, 0x03, %0, x" #vs1 ", x0" : "=r"(_khs_r)); \
    _khs_r; })

/* vd <- 32-bit lanes 0.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khs_vsldw0(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x00, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 1.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khs_vsldw1(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x01, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 2.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khs_vsldw2(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x02, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 3.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khs_vsldw3(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x03, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 4.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khs_vsldw4(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x04, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 5.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khs_vsldw5(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x05, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 6.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khs_vsldw6(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x06, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 7.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khs_vsldw7(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x07, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- {vs2, vs1} narrowed int16 -> int8 with signed saturation */
#define khs_vpack_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x08, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- {vs2, vs1} narrowed int32 -> int16 with signed saturation */
#define khs_vpack_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x10, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- vs1's low int8s, widened to int16 */
#define khs_vunpkl_s8(vd, vs1) \
    __asm__ volatile(".insn r 0x0b, 7, 0x18, x" #vd ", x" #vs1 ", x0")

/* vd <- vs1's high int8s, widened to int16 */
#define khs_vunpkh_s8(vd, vs1) \
    __asm__ volatile(".insn r 0x0b, 7, 0x20, x" #vd ", x" #vs1 ", x0")

/* vd <- vs1's low int16s, widened to int32 */
#define khs_vunpkl_s16(vd, vs1) \
    __asm__ volatile(".insn r 0x0b, 7, 0x28, x" #vd ", x" #vs1 ", x0")

/* vd <- vs1's high int16s, widened to int32 */
#define khs_vunpkh_s16(vd, vs1) \
    __asm__ volatile(".insn r 0x0b, 7, 0x30, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- vs1[i] * vs2[i] over f16. The lane's addend is forced to zero rather than a second multiplier being built. A 256-bit register is 16 f16 elements; the element count is the register width over the operand width, not the lane count. */
#define khs_vfmul_f16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x00, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- vs1[i] + vs2[i] over f16. The lane's multiplier is forced to 1.0 rather than a second adder being built. A 256-bit register is 16 f16 elements; the element count is the register width over the operand width, not the lane count. */
#define khs_vfadd_f16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x04, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- vs1[i] - vs2[i] over f16. vs2's SIGN BIT is inverted and the add proceeds: negating a float is one bit, not a subtractor. A 256-bit register is 16 f16 elements; the element count is the register width over the operand width, not the lane count. */
#define khs_vfsub_f16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x08, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- vs1[i] * vs2[i] + vd[i] over f16, rounded ONCE. vd is read as the addend and then written, which is what makes this one fused operation rather than two instructions. A 256-bit register is 16 f16 elements; the element count is the register width over the operand width, not the lane count. */
#define khs_vfma_f16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x0c, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- min(vs1[i], vs2[i]) over f16. The winner is selected at the lane's first cycle and sent through as winner*1.0 + 0, which is bit-exact. A 256-bit register is 16 f16 elements; the element count is the register width over the operand width, not the lane count. */
#define khs_vfmin_f16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x10, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- max(vs1[i], vs2[i]) over f16. A 256-bit register is 16 f16 elements; the element count is the register width over the operand width, not the lane count. */
#define khs_vfmax_f16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x14, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- all ones if vs1[i] < vs2[i] else all zeros, over f16. A MASK IN AN ORDINARY VECTOR REGISTER, so vand/vandn/vor do the blend and a branchless conditional needs no new architectural state. NaN compares false in every form. A 256-bit register is 16 f16 elements; the element count is the register width over the operand width, not the lane count. */
#define khs_vfcmplt_f16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x18, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- all ones if vs1[i] > vs2[i] else all zeros, over f16. A MASK IN AN ORDINARY VECTOR REGISTER, so vand/vandn/vor do the blend and a branchless conditional needs no new architectural state. NaN compares false in every form. A 256-bit register is 16 f16 elements; the element count is the register width over the operand width, not the lane count. */
#define khs_vfcmpgt_f16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x1c, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- all ones if vs1[i] == vs2[i] else all zeros, over f16. A MASK IN AN ORDINARY VECTOR REGISTER, so vand/vandn/vor do the blend and a branchless conditional needs no new architectural state. NaN compares false in every form. A 256-bit register is 16 f16 elements; the element count is the register width over the operand width, not the lane count. */
#define khs_vfcmpeq_f16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x20, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- vs1[i] * vs2[i] over f32. The lane's addend is forced to zero rather than a second multiplier being built. A 256-bit register is 8 f32 elements. */
#define khs_vfmul_f32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x01, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- vs1[i] + vs2[i] over f32. The lane's multiplier is forced to 1.0 rather than a second adder being built. A 256-bit register is 8 f32 elements. */
#define khs_vfadd_f32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x05, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- vs1[i] - vs2[i] over f32. vs2's SIGN BIT is inverted and the add proceeds: negating a float is one bit, not a subtractor. A 256-bit register is 8 f32 elements. */
#define khs_vfsub_f32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x09, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- vs1[i] * vs2[i] + vd[i] over f32, rounded ONCE. vd is read as the addend and then written, which is what makes this one fused operation rather than two instructions. A 256-bit register is 8 f32 elements. */
#define khs_vfma_f32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x0d, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- min(vs1[i], vs2[i]) over f32. The winner is selected at the lane's first cycle and sent through as winner*1.0 + 0, which is bit-exact. A 256-bit register is 8 f32 elements. */
#define khs_vfmin_f32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x11, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- max(vs1[i], vs2[i]) over f32. A 256-bit register is 8 f32 elements. */
#define khs_vfmax_f32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x15, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- all ones if vs1[i] < vs2[i] else all zeros, over f32. A MASK IN AN ORDINARY VECTOR REGISTER, so vand/vandn/vor do the blend and a branchless conditional needs no new architectural state. NaN compares false in every form. A 256-bit register is 8 f32 elements. */
#define khs_vfcmplt_f32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x19, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- all ones if vs1[i] > vs2[i] else all zeros, over f32. A MASK IN AN ORDINARY VECTOR REGISTER, so vand/vandn/vor do the blend and a branchless conditional needs no new architectural state. NaN compares false in every form. A 256-bit register is 8 f32 elements. */
#define khs_vfcmpgt_f32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x1d, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- all ones if vs1[i] == vs2[i] else all zeros, over f32. A MASK IN AN ORDINARY VECTOR REGISTER, so vand/vandn/vor do the blend and a branchless conditional needs no new architectural state. NaN compares false in every form. A 256-bit register is 8 f32 elements. */
#define khs_vfcmpeq_f32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 3, 0x21, x" #vd ", x" #vs1 ", x" #vs2)

/* vd[i] <- (int32)vs1[i], truncating toward zero, where vs1 holds f16 elements. Saturates at int32's bounds; a NaN gives zero. */
#define khs_vfcvt_f2i_f16(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 2, 0x00, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- (f16)(int32)vs1[i], round to nearest even. */
#define khs_vfcvt_i2f_f16(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 2, 0x04, x" #vd ", x" #vs1 ", x0")

/* vd <- vs1 converted to f16: the element type names the DESTINATION, so .f32 widens f16->f32 (exact -- E8 is FP32's exponent verbatim) and .f16 narrows f32->f16 (rounds, and a finite overflow saturates rather than becoming an infinity). */
#define khs_vfcvt_f2f_f16(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 2, 0x08, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- (int32)vs1[i], truncating toward zero, where vs1 holds f32 elements. Saturates at int32's bounds; a NaN gives zero. */
#define khs_vfcvt_f2i_f32(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 2, 0x01, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- (f32)(int32)vs1[i], round to nearest even. */
#define khs_vfcvt_i2f_f32(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 2, 0x05, x" #vd ", x" #vs1 ", x0")

/* vd <- vs1 converted to f32: the element type names the DESTINATION, so .f32 widens f16->f32 (exact -- E8 is FP32's exponent verbatim) and .f16 narrows f32->f16 (rounds, and a finite overflow saturates rather than becoming an infinity). */
#define khs_vfcvt_f2f_f32(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 2, 0x09, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- 2 raised to vs1[i], over f16. Full rate, II=1, through the same normaliser and rounder the FMA uses. */
#define khs_vfexp2_f16(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 4, 0x00, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- the base-2 logarithm of vs1[i], over f16. Full rate, II=1, through the same normaliser and rounder the FMA uses. */
#define khs_vflog2_f16(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 4, 0x04, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- 1 / vs1[i], over f16. Full rate, II=1, through the same normaliser and rounder the FMA uses. */
#define khs_vfrcp_f16(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 4, 0x08, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- 1 / sqrt(vs1[i]), over f16. Full rate, II=1, through the same normaliser and rounder the FMA uses. */
#define khs_vfrsqrt_f16(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 4, 0x0c, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- 2 raised to vs1[i], over f32. Full rate, II=1, through the same normaliser and rounder the FMA uses. */
#define khs_vfexp2_f32(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 4, 0x01, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- the base-2 logarithm of vs1[i], over f32. Full rate, II=1, through the same normaliser and rounder the FMA uses. */
#define khs_vflog2_f32(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 4, 0x05, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- 1 / vs1[i], over f32. Full rate, II=1, through the same normaliser and rounder the FMA uses. */
#define khs_vfrcp_f32(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 4, 0x09, x" #vd ", x" #vs1 ", x0")

/* vd[i] <- 1 / sqrt(vs1[i]), over f32. Full rate, II=1, through the same normaliser and rounder the FMA uses. */
#define khs_vfrsqrt_f32(vd, vs1) \
    __asm__ volatile(".insn r 0x2b, 4, 0x0d, x" #vd ", x" #vs1 ", x0")

/* facc[ad] += vs1 * vs2, elementwise over f16. Lands on the next rotating partial, so consecutive ones issue at II=1 despite a 15-deep lane; the order is contract. */
#define khs_vfmacc_f16(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 0, 0x00, x" #ad ", x" #vs1 ", x" #vs2)

/* facc[ad][i] -= vs1[i] * vs2[i], elementwise over f16 */
#define khs_vfmsac_f16(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 0, 0x04, x" #ad ", x" #vs1 ", x" #vs2)

/* facc[ad] <- vs1 -- how a bias vector seeds a float accumulation */
#define khs_vfaccwr_f16(ad, vs1) \
    __asm__ volatile(".insn r 0x2b, 0, 0x10, x" #ad ", x" #vs1 ", x0")

/* vd <- facc[as1], rounded and saturated back to f16 */
#define khs_vfaccrd_f16(vd, as1) \
    __asm__ volatile(".insn r 0x2b, 0, 0x0c, x" #vd ", x" #as1 ", x0")

/* xd <- the sum of every slot of facc[as1]. Serial through one lane's adder: it runs once per kernel, and a float adder tree would be four normalisers and four rounders for that. */
#define khs_vfredsum_f16(as1) ({ \
    int32_t _khs_r; \
    __asm__ volatile(".insn r 0x2b, 1, 0x00, %0, x" #as1 ", x0" : "=r"(_khs_r)); \
    _khs_r; })

/* facc[ad] += vs1 * vs2, elementwise over f32. Lands on the next rotating partial, so consecutive ones issue at II=1 despite a 15-deep lane; the order is contract. */
#define khs_vfmacc_f32(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 0, 0x01, x" #ad ", x" #vs1 ", x" #vs2)

/* facc[ad][i] -= vs1[i] * vs2[i], elementwise over f32 */
#define khs_vfmsac_f32(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 0, 0x05, x" #ad ", x" #vs1 ", x" #vs2)

/* facc[ad] <- vs1 -- how a bias vector seeds a float accumulation */
#define khs_vfaccwr_f32(ad, vs1) \
    __asm__ volatile(".insn r 0x2b, 0, 0x11, x" #ad ", x" #vs1 ", x0")

/* vd <- facc[as1], widened to f32 exactly */
#define khs_vfaccrd_f32(vd, as1) \
    __asm__ volatile(".insn r 0x2b, 0, 0x0d, x" #vd ", x" #as1 ", x0")

/* xd <- the sum of every slot of facc[as1]. Serial through one lane's adder: it runs once per kernel, and a float adder tree would be four normalisers and four rounders for that. */
#define khs_vfredsum_f32(as1) ({ \
    int32_t _khs_r; \
    __asm__ volatile(".insn r 0x2b, 1, 0x01, %0, x" #as1 ", x0" : "=r"(_khs_r)); \
    _khs_r; })

/* facc[ad] <- 0, every slot. Untyped: zero is zero in either format. */
#define khs_vfaccz(ad) \
    __asm__ volatile(".insn r 0x2b, 0, 0x08, x" #ad ", x0, x0")

#endif /* KHS_INTRIN_H */
