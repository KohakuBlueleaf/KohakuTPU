/* GENERATED from tests/pe/tools/rv_dsp_isa.py -- DO NOT EDIT.
 * Regenerate with `python tests/pe/tools/rv_dsp_emit.py`; the ISA test
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
#ifndef KHD_INTRIN_H
#define KHD_INTRIN_H

#include <stdint.h>

#define KHD_VSPAD_BASE 0x40000000u

/* vd <- the vector at rs1+imm. Line-aligned by contract: an address that is not a multiple of the vector width faults. */
#define khd_vld(vd, p, imm) \
    __asm__ volatile(".insn i 0x0b, 0, x" #vd ", %0, " #imm : : "r"(p) : "memory")

/* the vector at rs1+imm <- vs. Line-aligned; a misaligned address faults. */
#define khd_vst(vs, p, imm) \
    __asm__ volatile(".insn i 0x0b, 1, x" #vs ", %0, " #imm : : "r"(p) : "memory")

/* element-wise wrapping add (s8 elements) */
#define khd_vadd_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x00, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise wrapping add (s16 elements) */
#define khd_vadd_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x01, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise wrapping add (s32 elements) */
#define khd_vadd_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x02, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise wrapping subtract (s8 elements) */
#define khd_vsub_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x04, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise wrapping subtract (s16 elements) */
#define khd_vsub_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x05, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise wrapping subtract (s32 elements) */
#define khd_vsub_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x06, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating add (s8 elements) */
#define khd_vsadd_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x08, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating add (s16 elements) */
#define khd_vsadd_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x09, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating add (s32 elements) */
#define khd_vsadd_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x0a, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating subtract (s8 elements) */
#define khd_vssub_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x0c, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating subtract (s16 elements) */
#define khd_vssub_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x0d, x" #vd ", x" #vs1 ", x" #vs2)

/* signed saturating subtract (s32 elements) */
#define khd_vssub_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x0e, x" #vd ", x" #vs1 ", x" #vs2)

/* signed minimum (s8 elements) */
#define khd_vmin_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x10, x" #vd ", x" #vs1 ", x" #vs2)

/* signed minimum (s16 elements) */
#define khd_vmin_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x11, x" #vd ", x" #vs1 ", x" #vs2)

/* signed minimum (s32 elements) */
#define khd_vmin_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x12, x" #vd ", x" #vs1 ", x" #vs2)

/* signed maximum (s8 elements) */
#define khd_vmax_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x14, x" #vd ", x" #vs1 ", x" #vs2)

/* signed maximum (s16 elements) */
#define khd_vmax_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x15, x" #vd ", x" #vs1 ", x" #vs2)

/* signed maximum (s32 elements) */
#define khd_vmax_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x16, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise product, low half kept (s8 elements) */
#define khd_vmul_s8(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x18, x" #vd ", x" #vs1 ", x" #vs2)

/* element-wise product, low half kept (s16 elements) */
#define khd_vmul_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 2, 0x19, x" #vd ", x" #vs1 ", x" #vs2)

/* bitwise and */
#define khd_vand(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 3, 0x00, x" #vd ", x" #vs1 ", x" #vs2)

/* bitwise or */
#define khd_vor(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 3, 0x01, x" #vd ", x" #vs1 ", x" #vs2)

/* bitwise exclusive or */
#define khd_vxor(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 3, 0x02, x" #vd ", x" #vs1 ", x" #vs2)

/* vs1 & ~vs2 */
#define khd_vandn(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 3, 0x03, x" #vd ", x" #vs1 ", x" #vs2)

/* shift left logical (s8 elements) */
#define khd_vslli_s8(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x00, x" #vd ", x" #vs1 ", x" #sh)

/* shift left logical (s16 elements) */
#define khd_vslli_s16(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x01, x" #vd ", x" #vs1 ", x" #sh)

/* shift left logical (s32 elements) */
#define khd_vslli_s32(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x02, x" #vd ", x" #vs1 ", x" #sh)

/* shift right logical (s8 elements) */
#define khd_vsrli_s8(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x04, x" #vd ", x" #vs1 ", x" #sh)

/* shift right logical (s16 elements) */
#define khd_vsrli_s16(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x05, x" #vd ", x" #vs1 ", x" #sh)

/* shift right logical (s32 elements) */
#define khd_vsrli_s32(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x06, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic (s8 elements) */
#define khd_vsrai_s8(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x08, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic (s16 elements) */
#define khd_vsrai_s16(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x09, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic (s32 elements) */
#define khd_vsrai_s32(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x0a, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic, ROUNDING (add half an ulp first) -- the requantise primitive, and the one a plain vsrai gets subtly wrong (s8 elements) */
#define khd_vsrari_s8(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x0c, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic, ROUNDING (add half an ulp first) -- the requantise primitive, and the one a plain vsrai gets subtly wrong (s16 elements) */
#define khd_vsrari_s16(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x0d, x" #vd ", x" #vs1 ", x" #sh)

/* shift right arithmetic, ROUNDING (add half an ulp first) -- the requantise primitive, and the one a plain vsrai gets subtly wrong (s32 elements) */
#define khd_vsrari_s32(vd, vs1, sh) \
    __asm__ volatile(".insn r 0x0b, 4, 0x0e, x" #vd ", x" #vs1 ", x" #sh)

/* acc[ad] += the dot product of the elements within each 32-bit lane (s8 elements) */
#define khd_vdot_s8(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 5, 0x00, x" #ad ", x" #vs1 ", x" #vs2)

/* acc[ad] += the dot product of the elements within each 32-bit lane (s16 elements) */
#define khd_vdot_s16(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 5, 0x01, x" #ad ", x" #vs1 ", x" #vs2)

/* acc[ad] -= the dot product of the elements within each 32-bit lane (s8 elements) */
#define khd_vdotn_s8(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 5, 0x04, x" #ad ", x" #vs1 ", x" #vs2)

/* acc[ad] -= the dot product of the elements within each 32-bit lane (s16 elements) */
#define khd_vdotn_s16(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 5, 0x05, x" #ad ", x" #vs1 ", x" #vs2)

/* acc[ad] <- 0 */
#define khd_vaccz(ad) \
    __asm__ volatile(".insn r 0x0b, 5, 0x08, x" #ad ", x0, x0")

/* vd <- acc[as1], as int32 lanes */
#define khd_vaccrd(vd, as1) \
    __asm__ volatile(".insn r 0x0b, 5, 0x0c, x" #vd ", x" #as1 ", x0")

/* acc[ad] <- vs1, as int32 lanes -- how a bias vector seeds an accumulation */
#define khd_vaccwr(ad, vs1) \
    __asm__ volatile(".insn r 0x0b, 5, 0x10, x" #ad ", x" #vs1 ", x0")

/* every 32-bit lane of vd <- xs1 */
#define khd_vsplat(vd, xs1) \
    __asm__ volatile(".insn r 0x0b, 6, 0x00, x" #vd ", %0, x0" : : "r"(xs1))

/* xd <- 32-bit lane `sh` of vs1 */
#define khd_vextr(vs1, sh) ({ \
    int32_t _khd_r; \
    __asm__ volatile(".insn r 0x0b, 6, 0x01, %0, x" #vs1 ", x" #sh : "=r"(_khd_r)); \
    _khd_r; })

/* xd <- the sum of vs1's 32-bit lanes */
#define khd_vredsum(vs1) ({ \
    int32_t _khd_r; \
    __asm__ volatile(".insn r 0x0b, 6, 0x02, %0, x" #vs1 ", x0" : "=r"(_khd_r)); \
    _khd_r; })

/* xd <- the signed max of vs1's 32-bit lanes */
#define khd_vredmax(vs1) ({ \
    int32_t _khd_r; \
    __asm__ volatile(".insn r 0x0b, 6, 0x03, %0, x" #vs1 ", x0" : "=r"(_khd_r)); \
    _khd_r; })

/* vd <- 32-bit lanes 0.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khd_vsldw0(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x00, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 1.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khd_vsldw1(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x01, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 2.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khd_vsldw2(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x02, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 3.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khd_vsldw3(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x03, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 4.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khd_vsldw4(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x04, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 5.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khd_vsldw5(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x05, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 6.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khd_vsldw6(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x06, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- 32-bit lanes 7.. of the concatenation {vs2, vs1} -- the misaligned-neighbour primitive a stencil needs */
#define khd_vsldw7(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x07, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- {vs2, vs1} narrowed int16 -> int8 with signed saturation */
#define khd_vpack_s16(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x08, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- {vs2, vs1} narrowed int32 -> int16 with signed saturation */
#define khd_vpack_s32(vd, vs1, vs2) \
    __asm__ volatile(".insn r 0x0b, 7, 0x10, x" #vd ", x" #vs1 ", x" #vs2)

/* vd <- vs1's low int8s, widened to int16 */
#define khd_vunpkl_s8(vd, vs1) \
    __asm__ volatile(".insn r 0x0b, 7, 0x18, x" #vd ", x" #vs1 ", x0")

/* vd <- vs1's high int8s, widened to int16 */
#define khd_vunpkh_s8(vd, vs1) \
    __asm__ volatile(".insn r 0x0b, 7, 0x20, x" #vd ", x" #vs1 ", x0")

/* vd <- vs1's low int16s, widened to int32 */
#define khd_vunpkl_s16(vd, vs1) \
    __asm__ volatile(".insn r 0x0b, 7, 0x28, x" #vd ", x" #vs1 ", x0")

/* vd <- vs1's high int16s, widened to int32 */
#define khd_vunpkh_s16(vd, vs1) \
    __asm__ volatile(".insn r 0x0b, 7, 0x30, x" #vd ", x" #vs1 ", x0")

/* facc[ad] += vs1 * vs2, elementwise over f16. Lands on the next rotating partial, so consecutive ones issue at II=1 despite a 14-deep lane; the order is contract. */
#define khd_vfmacc_f16(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 0, 0x00, x" #ad ", x" #vs1 ", x" #vs2)

/* facc[ad][i] -= vs1[i] * vs2[i], elementwise over f16 */
#define khd_vfmsac_f16(ad, vs1, vs2) \
    __asm__ volatile(".insn r 0x2b, 0, 0x04, x" #ad ", x" #vs1 ", x" #vs2)

/* facc[ad] <- vs1 -- how a bias vector seeds a float accumulation */
#define khd_vfaccwr_f16(ad, vs1) \
    __asm__ volatile(".insn r 0x2b, 0, 0x10, x" #ad ", x" #vs1 ", x0")

/* vd <- facc[as1], rounded and saturated back to f16 */
#define khd_vfaccrd_f16(vd, as1) \
    __asm__ volatile(".insn r 0x2b, 0, 0x0c, x" #vd ", x" #as1 ", x0")

/* xd <- the sum of every slot of facc[as1]. Serial through one lane's adder: it runs once per kernel, and a float adder tree would be four normalisers and four rounders for that. */
#define khd_vfredsum_f16(as1) ({ \
    int32_t _khd_r; \
    __asm__ volatile(".insn r 0x2b, 1, 0x00, %0, x" #as1 ", x0" : "=r"(_khd_r)); \
    _khd_r; })

/* facc[ad] <- 0, every slot. Untyped: zero is zero in either format. */
#define khd_vfaccz(ad) \
    __asm__ volatile(".insn r 0x2b, 0, 0x08, x" #ad ", x0, x0")

#endif /* KHD_INTRIN_H */
