/* The queue service: the dispatcher's main loop. */
#ifndef KA_QUEUE_SERVICE_H
#define KA_QUEUE_SERVICE_H

#include <stdint.h>

/* Serve the queue named by the boot block until a STOP entry; returns the
 * exit word (KA_EXIT_STOP | value), or a nonzero setup failure. */
uint64_t ka_queue_serve(void);

#endif
