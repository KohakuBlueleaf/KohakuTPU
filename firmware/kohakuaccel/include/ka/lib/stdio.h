/* stdio for the node: bytes out to the sink (else the console tap), bytes in
 * from the source (else the control region's stdin register). */
#ifndef KA_LIB_STDIO_H
#define KA_LIB_STDIO_H

#include <stdarg.h>
#include <stdint.h>

typedef void (*ka_sink_fn)(const char *buf, unsigned n);
typedef int (*ka_source_fn)(void); /* a byte, or -1 when none is waiting */

void ka_set_sink(ka_sink_fn fn);     /* replaces the console tap for all output */
void ka_debug(const char *s);        /* the console tap, directly: short debug only */
void ka_set_source(ka_source_fn fn);

void ka_putc(char c);
void ka_puts(const char *s);
void ka_flush(void);
int ka_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
int ka_vprintf(const char *fmt, va_list ap);

int ka_getc_nb(void); /* -1 when nothing is waiting */
int ka_getc(void);    /* blocks */

#endif
