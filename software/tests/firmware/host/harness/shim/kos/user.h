/* The user API with its system-call stub compiled for no machine: `register` off,
 * the stub's a0..a7 name no register here. Harnesses define what they call. */
#ifndef SHIM_KOS_USER_H
#define SHIM_KOS_USER_H

#define register
#include_next <kos/user.h>
#undef register

#endif
