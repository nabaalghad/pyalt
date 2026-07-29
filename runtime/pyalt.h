/* pyalt runtime — included by every generated C program.
 *
 * Types: int -> int64_t, float -> double, bool -> bool,
 *        str -> PStr* (immutable), list[T] -> PList* (8-byte PVal slots),
 *        dict[K,V] / set[T] -> PDict* (insertion-ordered hash table).
 *
 * Memory: managed by a built-in conservative mark-sweep GC (see the
 * "memory" section below): bump allocation into 4 MB chunks, stack/register
 * roots, interior-pointer support for zero-copy string views, size-class
 * free lists, immortal literals. Knobs: PYA_GC=off, PYA_GC_MIN=<bytes>.
 * All indexing is bounds-checked; any runtime violation calls pya_die()
 * -> message on stderr, exit code 1 (or RuntimeError when embedded in
 * Python via the pya_die_hook).
 *
 * Semantics match Python where they differ from C: // floors, % follows the
 * divisor's sign, / always yields float and checks for zero; dict/set
 * iteration is in insertion order.
 */
#ifndef PYA_RUNTIME_H
#define PYA_RUNTIME_H

#if defined(__linux__) && !defined(_GNU_SOURCE)
#define _GNU_SOURCE /* for pthread_getattr_np (GC stack bounds) */
#endif

#define _CRT_SECURE_NO_WARNINGS
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <setjmp.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h> /* CommandLineToArgvW */
#include <process.h>
#ifdef _MSC_VER
#pragma comment(lib, "shell32.lib")
#endif
#elif defined(__linux__) || defined(__APPLE__)
#include <pthread.h>
#include <unistd.h>
#endif

#ifdef _MSC_VER
#define PYA_TLS __declspec(thread)
#else
#define PYA_TLS _Thread_local
#endif

/* one lock guards the shared allocator state (chunk list, heap bounds,
 * gc_since); the hot bump path inside a thread's own chunk is lock-free */
#ifdef _WIN32
static SRWLOCK pya_gc_lock = SRWLOCK_INIT;
#define PYA_GC_LOCK() AcquireSRWLockExclusive(&pya_gc_lock)
#define PYA_GC_UNLOCK() ReleaseSRWLockExclusive(&pya_gc_lock)
#else
static pthread_mutex_t pya_gc_lock = PTHREAD_MUTEX_INITIALIZER;
#define PYA_GC_LOCK() pthread_mutex_lock(&pya_gc_lock)
#define PYA_GC_UNLOCK() pthread_mutex_unlock(&pya_gc_lock)
#endif

static volatile int pya_in_parallel = 0;

#ifdef _MSC_VER
#define PYA_NORETURN __declspec(noreturn)
#else
#define PYA_NORETURN __attribute__((noreturn))
#endif

#define PYA_END INT64_MAX /* sentinel for an omitted slice upper bound */

/* When embedded (e.g. as a Python extension module), the host installs a hook
 * that longjmps out instead of killing the process. The hook must not return;
 * if it does, we still exit. */
static void (*pya_die_hook)(const char *msg) = NULL;

/* try/except: a thread-local stack of setjmp frames. pya_die unwinds to the
 * innermost frame if one exists; otherwise falls through to the embedding
 * hook (Python RuntimeError) or aborts. */
typedef struct PyaTryFrame {
    jmp_buf jb;
    struct PyaTryFrame *prev;
} PyaTryFrame;

static PYA_TLS PyaTryFrame *pya_try_top = NULL;
static PYA_TLS char pya_exc_buf[512]; /* message of the in-flight error;
    copied here because the original may live on an abandoned stack frame */

static PYA_NORETURN void pya_die(const char *msg) {
    if (pya_try_top) {
        if (msg != pya_exc_buf) {
            strncpy(pya_exc_buf, msg, sizeof pya_exc_buf - 1);
            pya_exc_buf[sizeof pya_exc_buf - 1] = '\0';
        }
        longjmp(pya_try_top->jb, 1);
    }
    /* the embedding hook longjmps — never valid from a worker thread */
    if (pya_die_hook && !pya_in_parallel) pya_die_hook(msg);
    fprintf(stderr, "pyalt runtime error: %s\n", msg);
    exit(1);
}

/* field access on a class instance that was never assigned */
static void *pya_fld(void *p) {
    if (!p) pya_die("field access on an uninitialized instance");
    return p;
}

/* ---------------- memory: conservative mark-sweep GC ----------------
 *
 * Bump allocation into 4 MB chunks (fast path unchanged from the old
 * allocator) + a Boehm-style conservative collector on top:
 *   - every object has an 8-byte header (size | mark/free/prefix flags)
 *   - each chunk keeps an ascending table of object offsets, so any pointer
 *     INTO an object (our zero-copy string views!) resolves to its object
 *   - roots = the C stack (bounds from the OS) + callee-saved registers
 *     (captured via setjmp); marking scans object payloads for pointer-like
 *     words; strings scan only their 24-byte struct prefix, not their text
 *   - sweep pushes dead objects onto size-class free lists for reuse
 *   - everything allocated before pya_gc_seal() (string literals, singletons)
 *     is immortal and never scanned or swept
 * Knobs: PYA_GC=off disables collection; PYA_GC_MIN=<bytes> sets the minimum
 * allocation volume between collections (default 64 MB).
 */

#define GC_MARK   (1ULL << 63)
#define GC_FREE   (1ULL << 62)
#define GC_PREFIX (1ULL << 61) /* scan only the first 24 bytes (PStr) */
#define GC_SIZE(h) ((int64_t)((h) & ((1ULL << 48) - 1)))
#define GC_HDR(payload) (((uint64_t *)(payload)) - 1)

typedef struct GcChunk {
    struct GcChunk *next;
    char *base, *bump, *end;
    uint32_t *offs;          /* ascending header offsets from base */
    int64_t noffs, coffs;
    bool immortal;
} GcChunk;

static const int64_t GC_CLASSES[] = {16, 32, 48, 64, 96, 128, 192, 256,
                                     384, 512, 768, 1024, 1536, 2048, 3072, 4096};
#define GC_NCLASSES 16

static GcChunk *gc_chunks = NULL;        /* all chunks, newest first */
static PYA_TLS GcChunk *gc_cur = NULL;   /* each thread bumps its own chunk */
static void *gc_free_lists[GC_NCLASSES];
static void *gc_large_free = NULL;  /* payloads > 4096, linked by first word */
static bool gc_booted = false;
static bool gc_sealed = false;
static bool gc_off = false;
static int64_t gc_min_between = 256 << 20; /* batch-friendly; PYA_GC_MIN tunes */
static int64_t gc_live = 1 << 20;   /* live-bytes estimate after last sweep */
static int64_t gc_since = 0;        /* bytes allocated since last sweep */
static char *gc_heap_lo = (char *)UINTPTR_MAX; /* quick-reject bounds */
static char *gc_heap_hi = NULL;
static uint8_t gc_class_of[257];    /* (size>>4) -> free-list class index */

static void gc_build_class_table(void) {
    for (int i = 0; i <= 256; i++) {
        int64_t need = (int64_t)i << 4;
        uint8_t cls = GC_NCLASSES;
        for (int c = 0; c < GC_NCLASSES; c++) {
            if (GC_CLASSES[c] >= need) { cls = (uint8_t)c; break; }
        }
        gc_class_of[i] = cls;
    }
}

static char *pya_stack_high(void) {
#ifdef _WIN32
    ULONG_PTR lo, hi;
    GetCurrentThreadStackLimits(&lo, &hi);
    return (char *)hi;
#elif defined(__linux__)
    pthread_attr_t a;
    if (pthread_getattr_np(pthread_self(), &a) == 0) {
        void *addr; size_t sz;
        pthread_attr_getstack(&a, &addr, &sz);
        pthread_attr_destroy(&a);
        return (char *)addr + sz;
    }
    return NULL;
#elif defined(__APPLE__)
    return (char *)pthread_get_stackaddr_np(pthread_self());
#else
    return NULL;
#endif
}

static void gc_boot(void) {
    gc_booted = true;
    gc_build_class_table();
    const char *e = getenv("PYA_GC");
    if (e && strcmp(e, "off") == 0) gc_off = true;
    const char *m = getenv("PYA_GC_MIN");
    if (m) {
        long long v = atoll(m);
        if (v > 0) gc_min_between = (int64_t)v;
    }
    if (!pya_stack_high()) gc_off = true; /* no stack bounds -> no collection */
}

static GcChunk *gc_new_chunk(size_t payload) {
    GcChunk *c = (GcChunk *)malloc(sizeof(GcChunk) + payload);
    if (!c) pya_die("out of memory");
    c->base = (char *)(c + 1);
    c->bump = c->base;
    c->end = c->base + payload;
    c->offs = NULL;
    c->noffs = c->coffs = 0;
    c->immortal = !gc_sealed;
    PYA_GC_LOCK();
    if (c->base < gc_heap_lo) gc_heap_lo = c->base;
    if (c->end > gc_heap_hi) gc_heap_hi = c->end;
    c->next = gc_chunks;
    gc_chunks = c;
    gc_since += (int64_t)payload; /* heap growth drives the GC trigger */
    PYA_GC_UNLOCK();
    return c;
}

static void gc_collect_now(void); /* forward */

static void *pya_alloc_flags(size_t n, uint64_t flags) {
    if (!gc_booted) gc_boot();
    int64_t need = (int64_t)((n + 15u) & ~(size_t)15u);
    if (need == 0) need = 16;
    /* collection + free-list reuse are main-thread-only; workers bump into
     * their own thread-local chunks and never contend */
    if (!pya_in_parallel) {
        if (!gc_off && gc_sealed && gc_since > gc_min_between
                && gc_since > gc_live * 2) {
            gc_collect_now();
        }
        if (need <= 4096) {
            int c = gc_class_of[need >> 4];
            if (c < GC_NCLASSES && gc_free_lists[c]) {
                void *payload = gc_free_lists[c];
                gc_free_lists[c] = *(void **)payload;
                uint64_t *hdr = GC_HDR(payload);
                *hdr = (uint64_t)GC_SIZE(*hdr) | flags;
                return payload;
            }
        } else {
            void **prev = &gc_large_free;
            for (void *p = gc_large_free; p; p = *(void **)p) {
                int64_t sz = GC_SIZE(*GC_HDR(p));
                if (sz >= need && sz <= need * 2) {
                    *prev = *(void **)p;
                    uint64_t *hdr = GC_HDR(p);
                    *hdr = (uint64_t)sz | flags;
                    return p;
                }
                prev = (void **)p;
            }
        }
    }
    /* bump-allocate */
    int64_t total = 8 + need;
    if (!gc_cur || gc_cur->immortal != !gc_sealed
            || gc_cur->end - gc_cur->bump < total) {
        size_t payload = 4u << 20;
        if ((size_t)total > payload) payload = (size_t)total;
        gc_cur = gc_new_chunk(payload);
    }
    GcChunk *c = gc_cur;
    uint64_t *hdr = (uint64_t *)c->bump;
    *hdr = (uint64_t)need | flags;
    void *payload = c->bump + 8;
    if (c->noffs == c->coffs) {
        c->coffs = c->coffs ? c->coffs * 2 : 256;
        c->offs = (uint32_t *)realloc(c->offs, sizeof(uint32_t) * (size_t)c->coffs);
        if (!c->offs) pya_die("out of memory");
    }
    c->offs[c->noffs++] = (uint32_t)(c->bump - c->base);
    c->bump += total;
    return payload;
}

static void *pya_alloc(size_t n) { return pya_alloc_flags(n, 0); }

/* ---- collection ---- */

static GcChunk **gc_sorted = NULL;  /* mortal chunks sorted by base */
static int gc_nsorted = 0;
static void **gc_work = NULL;       /* mark worklist */
static int64_t gc_nwork = 0, gc_cwork = 0;

static int gc_cmp_chunk(const void *a, const void *b) {
    char *x = (*(GcChunk *const *)a)->base;
    char *y = (*(GcChunk *const *)b)->base;
    return x < y ? -1 : (x > y ? 1 : 0);
}

static void gc_push(void *payload) {
    if (gc_nwork == gc_cwork) {
        gc_cwork = gc_cwork ? gc_cwork * 2 : 4096;
        gc_work = (void **)realloc(gc_work, sizeof(void *) * (size_t)gc_cwork);
        if (!gc_work) pya_die("out of memory");
    }
    gc_work[gc_nwork++] = payload;
}

static GcChunk *gc_last_hit = NULL; /* one-entry cache: pointers cluster */

/* If p points anywhere inside a live mortal object, mark it and queue it. */
static void gc_try_mark(char *p) {
    if (p < gc_heap_lo || p >= gc_heap_hi) return; /* quick reject */
    GcChunk *c = gc_last_hit;
    if (!(c && p >= c->base && p < c->bump)) {
        int lo = 0, hi = gc_nsorted - 1, ci = -1;
        while (lo <= hi) {
            int mid = (lo + hi) / 2;
            GcChunk *cc = gc_sorted[mid];
            if (p < cc->base) hi = mid - 1;
            else if (p >= cc->bump) lo = mid + 1;
            else { ci = mid; break; }
        }
        if (ci < 0) return;
        c = gc_sorted[ci];
        gc_last_hit = c;
    }
    uint32_t off = (uint32_t)(p - c->base);
    int64_t l = 0, h = c->noffs - 1, oi = -1;
    while (l <= h) {
        int64_t mid = (l + h) / 2;
        if (c->offs[mid] <= off) { oi = mid; l = mid + 1; }
        else h = mid - 1;
    }
    if (oi < 0) return;
    uint64_t *hdr = (uint64_t *)(c->base + c->offs[oi]);
    if (p < (char *)hdr + 8 || p >= (char *)hdr + 8 + GC_SIZE(*hdr)) return;
    if (*hdr & (GC_MARK | GC_FREE)) return;
    *hdr |= GC_MARK;
    gc_push((char *)hdr + 8);
}

static void gc_scan_range(char *lo, char *hi) {
    lo = (char *)(((uintptr_t)lo + 7u) & ~(uintptr_t)7u);
    for (char *p = lo; p + 8 <= hi; p += 8)
        gc_try_mark(*(char **)p);
}

static void gc_collect_now(void) {
    /* 1. build the sorted mortal-chunk table and clear marks */
    int n = 0;
    for (GcChunk *c = gc_chunks; c; c = c->next)
        if (!c->immortal) n++;
    gc_sorted = (GcChunk **)realloc(gc_sorted, sizeof(GcChunk *) * (size_t)(n ? n : 1));
    if (!gc_sorted) pya_die("out of memory");
    gc_nsorted = 0;
    for (GcChunk *c = gc_chunks; c; c = c->next) {
        if (c->immortal) continue;
        gc_sorted[gc_nsorted++] = c;
        for (int64_t i = 0; i < c->noffs; i++)
            *(uint64_t *)(c->base + c->offs[i]) &= ~GC_MARK;
    }
    qsort(gc_sorted, (size_t)gc_nsorted, sizeof(GcChunk *), gc_cmp_chunk);
    gc_last_hit = NULL;
    /* 2. roots: callee-saved registers + the C stack */
    gc_nwork = 0;
    jmp_buf regs;
    setjmp(regs);
    gc_scan_range((char *)&regs, (char *)&regs + sizeof regs);
    char anchor;
    gc_scan_range(&anchor, pya_stack_high());
    /* 3. trace */
    while (gc_nwork > 0) {
        char *payload = (char *)gc_work[--gc_nwork];
        uint64_t h = *GC_HDR(payload);
        int64_t span = GC_SIZE(h);
        if ((h & GC_PREFIX) && span > 24) span = 24;
        gc_scan_range(payload, payload + span);
    }
    /* 4. sweep: dead objects go to the free lists */
    int64_t live = 0;
    for (int i = 0; i < gc_nsorted; i++) {
        GcChunk *c = gc_sorted[i];
        for (int64_t j = 0; j < c->noffs; j++) {
            uint64_t *hdr = (uint64_t *)(c->base + c->offs[j]);
            if (*hdr & GC_FREE) continue;
            int64_t sz = GC_SIZE(*hdr);
            if (*hdr & GC_MARK) {
                live += sz;
                continue;
            }
            *hdr = (uint64_t)sz | GC_FREE;
            void *payload = (char *)hdr + 8;
            if (sz <= 4096) {
                for (int cl = GC_NCLASSES - 1; cl >= 0; cl--) {
                    if (GC_CLASSES[cl] <= sz) {
                        *(void **)payload = gc_free_lists[cl];
                        gc_free_lists[cl] = payload;
                        break;
                    }
                }
            } else {
                *(void **)payload = gc_large_free;
                gc_large_free = payload;
            }
        }
    }
    gc_live = live > (1 << 20) ? live : (1 << 20);
    gc_since = 0;
}

/* Everything allocated so far (literals, singletons) becomes immortal. The
 * generated pya_init_literals() calls this last. */
typedef struct PStr PStr;
static PStr *pstr_empty(void); /* forward */

static void pya_gc_seal(void) {
    pstr_empty(); /* force the singleton into the immortal generation */
    for (GcChunk *c = gc_chunks; c; c = c->next)
        c->immortal = true;
    gc_cur = NULL;
    gc_sealed = true;
}

/* User-facing: force a collection, return live bytes. */
static int64_t pya_gc_collect(void) {
    if (!gc_booted || gc_off || !gc_sealed || pya_in_parallel) return gc_live;
    gc_collect_now();
    return gc_live;
}

/* ---------------- strings ---------------- */

struct PStr {
    int64_t len;
    char *data;    /* NUL-terminated for C convenience; len is authoritative */
    uint64_t hash; /* cached hash; 0 = not yet computed (strings are
                      immutable, so caching is safe — same trick as CPython) */
};

/* Header and character data in ONE allocation (data points just past the
 * struct). GC_PREFIX: the collector scans only the 24-byte struct part of
 * a string, never its text bytes. */
static PStr *pstr_raw(int64_t len) {
    PStr *s = (PStr *)pya_alloc_flags(sizeof(PStr) + (size_t)len + 1, GC_PREFIX);
    s->len = len;
    s->data = (char *)(s + 1);
    s->data[len] = '\0';
    s->hash = 0;
    return s;
}

static PStr *pstr_new(const char *bytes, int64_t len) {
    PStr *s = pstr_raw(len);
    if (len) memcpy(s->data, bytes, (size_t)len);
    return s;
}

/* Zero-copy view into an existing buffer. Safe because pyalt strings are
 * immutable and never freed. NOTE: views are NOT NUL-terminated — everything
 * in this runtime is length-based; the few C APIs that need NUL (strtoll,
 * strtod, fopen) go through pya_cbuf() below. */
static PStr *pstr_view(char *data, int64_t len) {
    PStr *s = (PStr *)pya_alloc_flags(sizeof(PStr), GC_PREFIX);
    s->len = len;
    s->data = data;
    s->hash = 0;
    return s;
}

/* Return a NUL-terminated char* for a possibly-view string. */
static const char *pya_cbuf(PStr *s) {
    return pstr_new(s->data, s->len)->data;
}

static PStr *pstr_from_c(const char *s) { return pstr_new(s, (int64_t)strlen(s)); }

static PStr *pstr_empty(void) {
    static PStr *e = NULL;
    if (!e) e = pstr_raw(0);
    return e;
}

/* user-level `raise "message"` */
static PYA_NORETURN void pya_raise(PStr *msg) {
    int64_t n = msg->len < (int64_t)sizeof pya_exc_buf - 1
                ? msg->len : (int64_t)sizeof pya_exc_buf - 1;
    memcpy(pya_exc_buf, msg->data, (size_t)n);
    pya_exc_buf[n] = '\0';
    pya_die(pya_exc_buf);
}

static PStr *pstr_concat(PStr *a, PStr *b) {
    if (a->len == 0) return b;
    if (b->len == 0) return a;
    PStr *s = pstr_raw(a->len + b->len);
    memcpy(s->data, a->data, (size_t)a->len);
    memcpy(s->data + a->len, b->data, (size_t)b->len);
    return s;
}

static PStr *pstr_repeat(PStr *a, int64_t n) {
    if (n <= 0 || a->len == 0) return pstr_empty();
    if (n == 1) return a;
    PStr *s = pstr_raw(a->len * n);
    for (int64_t i = 0; i < n; i++)
        memcpy(s->data + i * a->len, a->data, (size_t)a->len);
    return s;
}

static bool pstr_eq(PStr *a, PStr *b) {
    return a->len == b->len && memcmp(a->data, b->data, (size_t)a->len) == 0;
}

static int pstr_cmp(PStr *a, PStr *b) {
    int64_t n = a->len < b->len ? a->len : b->len;
    int c = n ? memcmp(a->data, b->data, (size_t)n) : 0;
    if (c) return c;
    return a->len < b->len ? -1 : (a->len > b->len ? 1 : 0);
}

static int64_t pstr_search(PStr *hay, PStr *needle, int64_t from) {
    if (needle->len == 0) return from <= hay->len ? from : -1;
    if (needle->len > hay->len) return -1;
    const char first = needle->data[0];
    const char *base = hay->data;
    int64_t last = hay->len - needle->len;
    while (from <= last) {
        const char *hit = (const char *)memchr(base + from, first,
                                               (size_t)(last - from + 1));
        if (!hit) return -1;
        int64_t i = hit - base;
        if (memcmp(base + i, needle->data, (size_t)needle->len) == 0)
            return i;
        from = i + 1;
    }
    return -1;
}

static bool pstr_contains(PStr *hay, PStr *needle) {
    return pstr_search(hay, needle, 0) >= 0;
}

static PStr *pstr_index(PStr *s, int64_t i) {
    if (i < 0) pya_die("negative string index (negative indexing is not in v1)");
    if (i >= s->len) pya_die("string index out of range");
    return pstr_view(s->data + i, 1);
}

static PStr *pstr_slice(PStr *s, int64_t lo, int64_t hi) {
    if (lo < 0) pya_die("negative slice index (negative indexing is not in v1)");
    if (lo > s->len) lo = s->len;
    if (hi > s->len) hi = s->len;
    if (hi < lo) hi = lo;
    return pstr_view(s->data + lo, hi - lo);
}

static bool pya_isspace(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

static PStr *pstr_strip(PStr *s) {
    int64_t a = 0, b = s->len;
    while (a < b && pya_isspace(s->data[a])) a++;
    while (b > a && pya_isspace(s->data[b - 1])) b--;
    if (a == 0 && b == s->len) return s; /* nothing to strip: zero-copy */
    return pstr_view(s->data + a, b - a);
}

static PStr *pstr_lower(PStr *s) {
    int64_t i = 0;
    while (i < s->len && !(s->data[i] >= 'A' && s->data[i] <= 'Z')) i++;
    if (i == s->len) return s; /* already lowercase: zero-copy */
    PStr *r = pstr_raw(s->len);
    memcpy(r->data, s->data, (size_t)i);
    for (; i < s->len; i++) { /* fused copy + transform, single pass */
        char c = s->data[i];
        r->data[i] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
    }
    return r;
}

static PStr *pstr_upper(PStr *s) {
    int64_t i = 0;
    while (i < s->len && !(s->data[i] >= 'a' && s->data[i] <= 'z')) i++;
    if (i == s->len) return s; /* already uppercase: zero-copy */
    PStr *r = pstr_raw(s->len);
    memcpy(r->data, s->data, (size_t)i);
    for (; i < s->len; i++) {
        char c = s->data[i];
        r->data[i] = (c >= 'a' && c <= 'z') ? (char)(c - 32) : c;
    }
    return r;
}

static bool pstr_startswith(PStr *s, PStr *p) {
    return p->len <= s->len && memcmp(s->data, p->data, (size_t)p->len) == 0;
}

static bool pstr_endswith(PStr *s, PStr *p) {
    return p->len <= s->len &&
           memcmp(s->data + (s->len - p->len), p->data, (size_t)p->len) == 0;
}

static int64_t pstr_find(PStr *s, PStr *needle) {
    if (needle->len == 0) return 0;
    return pstr_search(s, needle, 0);
}

static PStr *pstr_replace(PStr *s, PStr *old, PStr *repl) {
    if (old->len == 0) pya_die("str.replace() with an empty search string");
    int64_t first = pstr_search(s, old, 0);
    if (first < 0) return s; /* no occurrence: zero-copy */
    if (repl->len <= old->len) {
        /* result can only shrink: allocate once, single scan pass */
        PStr *r = pstr_raw(s->len);
        int64_t src = 0, dst = 0, hit = first;
        while (hit >= 0) {
            memcpy(r->data + dst, s->data + src, (size_t)(hit - src));
            dst += hit - src;
            memcpy(r->data + dst, repl->data, (size_t)repl->len);
            dst += repl->len;
            src = hit + old->len;
            hit = pstr_search(s, old, src);
        }
        memcpy(r->data + dst, s->data + src, (size_t)(s->len - src));
        dst += s->len - src;
        r->len = dst;
        r->data[dst] = '\0';
        return r;
    }
    /* growing replacement: count first, then build */
    int64_t count = 0, i = first;
    while (i >= 0) {
        count++;
        i = pstr_search(s, old, i + old->len);
    }
    PStr *r = pstr_raw(s->len + count * (repl->len - old->len));
    int64_t src = 0, dst = 0;
    while (src < s->len) {
        int64_t hit = pstr_search(s, old, src);
        int64_t stop = hit >= 0 ? hit : s->len;
        memcpy(r->data + dst, s->data + src, (size_t)(stop - src));
        dst += stop - src;
        src = stop;
        if (hit >= 0) {
            memcpy(r->data + dst, repl->data, (size_t)repl->len);
            dst += repl->len;
            src += old->len;
        }
    }
    return r;
}

/* ---------------- lists ---------------- */

typedef union {
    int64_t i;
    double f;
    void *p;
} PVal;

static PVal pval_i(int64_t v) { PVal x; x.i = v; return x; }
static PVal pval_f(double v)  { PVal x; x.f = v; return x; }
static PVal pval_p(void *v)   { PVal x; x.p = v; return x; }

typedef struct {
    int64_t len, cap;
    PVal *data;
} PList;

static PList *plist_new(int64_t cap) {
    PList *l = (PList *)pya_alloc(sizeof(PList));
    l->len = 0;
    l->cap = cap > 0 ? cap : 0;
    l->data = l->cap ? (PVal *)pya_alloc(sizeof(PVal) * (size_t)l->cap) : NULL;
    return l;
}

static void plist_append(PList *l, PVal v) {
    if (l->len == l->cap) {
        int64_t ncap = l->cap ? l->cap * 2 : 8;
        PVal *nd = (PVal *)pya_alloc(sizeof(PVal) * (size_t)ncap);
        if (l->len) memcpy(nd, l->data, sizeof(PVal) * (size_t)l->len);
        l->data = nd;
        l->cap = ncap;
    }
    l->data[l->len++] = v;
}

static PVal plist_get(PList *l, int64_t i) {
    if (i < 0) pya_die("negative list index (negative indexing is not in v1)");
    if (i >= l->len) pya_die("list index out of range");
    return l->data[i];
}

static void plist_set(PList *l, int64_t i, PVal v) {
    if (i < 0) pya_die("negative list index (negative indexing is not in v1)");
    if (i >= l->len) pya_die("list index out of range");
    l->data[i] = v;
}

static PVal plist_pop(PList *l) {
    if (l->len == 0) pya_die("pop from an empty list");
    return l->data[--l->len];
}

static PList *plist_concat(PList *a, PList *b) {
    PList *r = plist_new(a->len + b->len);
    if (a->len) memcpy(r->data, a->data, sizeof(PVal) * (size_t)a->len);
    if (b->len) memcpy(r->data + a->len, b->data, sizeof(PVal) * (size_t)b->len);
    r->len = a->len + b->len;
    return r;
}

static PList *plist_slice(PList *l, int64_t lo, int64_t hi) {
    if (lo < 0) pya_die("negative slice index (negative indexing is not in v1)");
    if (lo > l->len) lo = l->len;
    if (hi > l->len) hi = l->len;
    if (hi < lo) hi = lo;
    PList *r = plist_new(hi - lo);
    if (hi > lo) memcpy(r->data, l->data + lo, sizeof(PVal) * (size_t)(hi - lo));
    r->len = hi - lo;
    return r;
}

#include <stdarg.h>
static PList *plist_of(int64_t n, ...) {
    PList *l = plist_new(n);
    va_list ap;
    va_start(ap, n);
    for (int64_t i = 0; i < n; i++) l->data[i] = va_arg(ap, PVal);
    va_end(ap);
    l->len = n;
    return l;
}

static bool plist_contains_i(PList *l, int64_t v) {
    for (int64_t i = 0; i < l->len; i++)
        if (l->data[i].i == v) return true;
    return false;
}

static bool plist_contains_f(PList *l, double v) {
    for (int64_t i = 0; i < l->len; i++)
        if (l->data[i].f == v) return true;
    return false;
}

static bool plist_contains_s(PList *l, PStr *v) {
    const int64_t vlen = v->len;
    const char c0 = vlen ? v->data[0] : 0;
    for (int64_t i = 0; i < l->len; i++) {
        PStr *e = (PStr *)l->data[i].p;
        if (e->len == vlen &&
            (vlen == 0 || (e->data[0] == c0 &&
             memcmp(e->data + 1, v->data + 1, (size_t)(vlen - 1)) == 0)))
            return true;
    }
    return false;
}

static int pya_cmp_i(const void *a, const void *b) {
    int64_t x = ((const PVal *)a)->i, y = ((const PVal *)b)->i;
    return x < y ? -1 : (x > y ? 1 : 0);
}

static int pya_cmp_f(const void *a, const void *b) {
    double x = ((const PVal *)a)->f, y = ((const PVal *)b)->f;
    return x < y ? -1 : (x > y ? 1 : 0);
}

static int pya_cmp_s(const void *a, const void *b) {
    return pstr_cmp((PStr *)((const PVal *)a)->p, (PStr *)((const PVal *)b)->p);
}

static void plist_sort_i(PList *l) { qsort(l->data, (size_t)l->len, sizeof(PVal), pya_cmp_i); }
static void plist_sort_f(PList *l) { qsort(l->data, (size_t)l->len, sizeof(PVal), pya_cmp_f); }
static void plist_sort_s(PList *l) { qsort(l->data, (size_t)l->len, sizeof(PVal), pya_cmp_s); }

/* ---------------- dict / set ----------------
 * Compact insertion-ordered hash table (like CPython's dict): entries live in
 * an append-only array (giving Python-identical iteration order), a separate
 * open-addressing index maps hashes to entry slots. v2 has no deletion, so no
 * tombstones. Sets are dicts with unused values.
 * key kinds: 0 = int/bool, 1 = float, 2 = str
 */

typedef struct {
    PVal key;
    PVal val;
    int64_t dead;     /* deleted entries stay in place (insertion order) */
} PEntry;

typedef struct {
    int64_t len;      /* LIVE entries (must stay the first field: len(x)) */
    int64_t nentries; /* total entries incl. dead — the iteration bound */
    int64_t cap;      /* entries capacity */
    int64_t tombs;    /* tombstoned index slots */
    PEntry *entries;  /* insertion order */
    int64_t *index;   /* hash slot -> entry idx; -1 = empty, -2 = tombstone */
    int64_t mask;     /* index size - 1 (index size is a power of two) */
    int kind;
} PDict;

static uint64_t pya_hash_key(int kind, PVal v) {
    uint64_t x;
    if (kind == 2) { /* str: FNV-1a, cached in the string */
        PStr *s = (PStr *)v.p;
        if (s->hash) return s->hash;
        uint64_t h = 1469598103934665603ULL;
        for (int64_t i = 0; i < s->len; i++) {
            h ^= (unsigned char)s->data[i];
            h *= 1099511628211ULL;
        }
        if (h == 0) h = 1; /* 0 means "not computed" */
        s->hash = h;
        return h;
    }
    if (kind == 1) {
        double d = v.f;
        if (d == 0.0) d = 0.0; /* canonicalize -0.0 */
        memcpy(&x, &d, 8);
    } else {
        x = (uint64_t)v.i;
    }
    /* splitmix64 finalizer */
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

static bool pya_key_eq(int kind, PVal a, PVal b) {
    if (kind == 2) return pstr_eq((PStr *)a.p, (PStr *)b.p);
    if (kind == 1) return a.f == b.f;
    return a.i == b.i;
}

static PDict *pdict_new(int kind) {
    PDict *d = (PDict *)pya_alloc(sizeof(PDict));
    d->len = 0;
    d->nentries = 0;
    d->cap = 8;
    d->tombs = 0;
    d->entries = (PEntry *)pya_alloc(sizeof(PEntry) * 8);
    d->mask = 15;
    d->index = (int64_t *)pya_alloc(sizeof(int64_t) * 16);
    for (int64_t i = 0; i < 16; i++) d->index[i] = -1;
    d->kind = kind;
    return d;
}

static void pdict_rehash(PDict *d) {
    int64_t nsize = (d->mask + 1) * 2;
    d->index = (int64_t *)pya_alloc(sizeof(int64_t) * (size_t)nsize);
    d->mask = nsize - 1;
    d->tombs = 0;
    for (int64_t i = 0; i < nsize; i++) d->index[i] = -1;
    for (int64_t e = 0; e < d->nentries; e++) {
        if (d->entries[e].dead) continue;
        int64_t slot = (int64_t)(pya_hash_key(d->kind, d->entries[e].key)
                                 & (uint64_t)d->mask);
        while (d->index[slot] >= 0) slot = (slot + 1) & d->mask;
        d->index[slot] = e;
    }
}

/* returns entry idx >= 0 on hit; on miss returns -(insert_slot) - 1,
 * preferring the first tombstone seen on the probe path */
static int64_t pdict_probe(PDict *d, PVal key) {
    int64_t slot = (int64_t)(pya_hash_key(d->kind, key) & (uint64_t)d->mask);
    int64_t first_tomb = -1;
    for (;;) {
        int64_t e = d->index[slot];
        if (e == -1) {
            return -(first_tomb >= 0 ? first_tomb : slot) - 1;
        }
        if (e == -2) {
            if (first_tomb < 0) first_tomb = slot;
        } else if (pya_key_eq(d->kind, d->entries[e].key, key)) {
            return e;
        }
        slot = (slot + 1) & d->mask;
    }
}

/* slot of an existing key, or -1 (used by deletion) */
static int64_t pdict_find_slot(PDict *d, PVal key) {
    int64_t slot = (int64_t)(pya_hash_key(d->kind, key) & (uint64_t)d->mask);
    for (;;) {
        int64_t e = d->index[slot];
        if (e == -1) return -1;
        if (e >= 0 && pya_key_eq(d->kind, d->entries[e].key, key)) return slot;
        slot = (slot + 1) & d->mask;
    }
}

static void pdict_set(PDict *d, PVal key, PVal val) {
    if ((d->len + d->tombs) * 2 >= d->mask + 1) pdict_rehash(d);
    int64_t e = pdict_probe(d, key);
    if (e >= 0) {
        d->entries[e].val = val;
        return;
    }
    if (d->nentries == d->cap) {
        int64_t ncap = d->cap * 2;
        PEntry *ne = (PEntry *)pya_alloc(sizeof(PEntry) * (size_t)ncap);
        memcpy(ne, d->entries, sizeof(PEntry) * (size_t)d->nentries);
        d->entries = ne;
        d->cap = ncap;
    }
    int64_t slot = -e - 1;
    if (d->index[slot] == -2) d->tombs--;
    d->entries[d->nentries].key = key;
    d->entries[d->nentries].val = val;
    d->entries[d->nentries].dead = 0;
    d->index[slot] = d->nentries;
    d->nentries++;
    d->len++;
}

static PVal pdict_del(PDict *d, PVal key) {
    int64_t slot = pdict_find_slot(d, key);
    if (slot < 0) pya_die("dict key not found");
    int64_t e = d->index[slot];
    PVal v = d->entries[e].val;
    d->entries[e].dead = 1;
    d->index[slot] = -2;
    d->tombs++;
    d->len--;
    return v;
}

static PVal pdict_get(PDict *d, PVal key) {
    int64_t e = pdict_probe(d, key);
    if (e < 0) pya_die("dict key not found");
    return d->entries[e].val;
}

static PVal pdict_get_default(PDict *d, PVal key, PVal def) {
    int64_t e = pdict_probe(d, key);
    return e >= 0 ? d->entries[e].val : def;
}

static bool pdict_contains(PDict *d, PVal key) {
    return pdict_probe(d, key) >= 0;
}

static void pdict_add(PDict *d, PVal key) { pdict_set(d, key, pval_i(0)); }

static PList *pdict_keys(PDict *d) {
    PList *l = plist_new(d->len);
    for (int64_t i = 0; i < d->nentries; i++)
        if (!d->entries[i].dead) plist_append(l, d->entries[i].key);
    return l;
}

static PList *pdict_values(PDict *d) {
    PList *l = plist_new(d->len);
    for (int64_t i = 0; i < d->nentries; i++)
        if (!d->entries[i].dead) plist_append(l, d->entries[i].val);
    return l;
}

static PDict *pdict_of(int kind, int64_t n, ...) {
    PDict *d = pdict_new(kind);
    va_list ap;
    va_start(ap, n);
    for (int64_t i = 0; i < n; i++) {
        PVal k = va_arg(ap, PVal);
        PVal v = va_arg(ap, PVal);
        pdict_set(d, k, v);
    }
    va_end(ap);
    return d;
}

static PDict *pset_of(int kind, int64_t n, ...) {
    PDict *d = pdict_new(kind);
    va_list ap;
    va_start(ap, n);
    for (int64_t i = 0; i < n; i++) pdict_add(d, va_arg(ap, PVal));
    va_end(ap);
    return d;
}

/* ---------------- formatting / printing ---------------- */

static PStr *pya_fmt_i(int64_t v) {
    char buf[32];
    snprintf(buf, sizeof buf, "%lld", (long long)v);
    return pstr_from_c(buf);
}

static PStr *pya_fmt_f(double v) {
    char buf[64];
    if (isfinite(v) && v == floor(v) && fabs(v) < 1e16)
        snprintf(buf, sizeof buf, "%.1f", v);
    else
        snprintf(buf, sizeof buf, "%.12g", v);
    return pstr_from_c(buf);
}

static PStr *pya_fmt_b(bool v) { return pstr_from_c(v ? "True" : "False"); }

static void pya_print_s(PStr *s) { fwrite(s->data, 1, (size_t)s->len, stdout); }
static void pya_print_i(int64_t v) { printf("%lld", (long long)v); }
static void pya_print_f(double v) { pya_print_s(pya_fmt_f(v)); }
static void pya_print_b(bool v) { fputs(v ? "True" : "False", stdout); }
static void pya_print_sp(void) { fputc(' ', stdout); }
static void pya_print_nl(void) { fputc('\n', stdout); }

static void pya_print_list_i(PList *l) {
    fputc('[', stdout);
    for (int64_t i = 0; i < l->len; i++) {
        if (i) fputs(", ", stdout);
        pya_print_i(l->data[i].i);
    }
    fputc(']', stdout);
}

static void pya_print_list_f(PList *l) {
    fputc('[', stdout);
    for (int64_t i = 0; i < l->len; i++) {
        if (i) fputs(", ", stdout);
        pya_print_f(l->data[i].f);
    }
    fputc(']', stdout);
}

static void pya_print_list_b(PList *l) {
    fputc('[', stdout);
    for (int64_t i = 0; i < l->len; i++) {
        if (i) fputs(", ", stdout);
        pya_print_b(l->data[i].i != 0);
    }
    fputc(']', stdout);
}

static void pya_print_list_s(PList *l) {
    fputc('[', stdout);
    for (int64_t i = 0; i < l->len; i++) {
        if (i) fputs(", ", stdout);
        fputc('\'', stdout);
        pya_print_s((PStr *)l->data[i].p);
        fputc('\'', stdout);
    }
    fputc(']', stdout);
}

/* print kinds: 0 = int, 1 = float, 2 = bool, 3 = str (quoted, repr-style) */
static void pya_print_val(int pk, PVal v) {
    switch (pk) {
    case 0: pya_print_i(v.i); break;
    case 1: pya_print_f(v.f); break;
    case 2: pya_print_b(v.i != 0); break;
    default:
        fputc('\'', stdout);
        pya_print_s((PStr *)v.p);
        fputc('\'', stdout);
    }
}

static void pya_print_dict(PDict *d, int pk, int pv) {
    fputc('{', stdout);
    int64_t shown = 0;
    for (int64_t i = 0; i < d->nentries; i++) {
        if (d->entries[i].dead) continue;
        if (shown++) fputs(", ", stdout);
        pya_print_val(pk, d->entries[i].key);
        fputs(": ", stdout);
        pya_print_val(pv, d->entries[i].val);
    }
    fputc('}', stdout);
}

static void pya_print_set(PDict *d, int pk) {
    if (d->len == 0) { /* Python prints the empty set as set() */
        fputs("set()", stdout);
        return;
    }
    fputc('{', stdout);
    int64_t shown = 0;
    for (int64_t i = 0; i < d->nentries; i++) {
        if (d->entries[i].dead) continue;
        if (shown++) fputs(", ", stdout);
        pya_print_val(pk, d->entries[i].key);
    }
    fputc('}', stdout);
}

/* ---------------- conversions ---------------- */

/* Numbers arrive as views (e.g. CSV fields); strtoll/strtod need NUL, so
 * bounce short strings through a stack buffer. */
static const char *pya_numbuf(PStr *s, char *buf, size_t bufsize) {
    if ((size_t)s->len < bufsize) {
        memcpy(buf, s->data, (size_t)s->len);
        buf[s->len] = '\0';
        return buf;
    }
    return pya_cbuf(s);
}

static int64_t pya_str_to_i(PStr *s) {
    char buf[64];
    const char *p = pya_numbuf(s, buf, sizeof buf);
    char *end = NULL;
    while (*p == ' ' || *p == '\t') p++;
    long long v = strtoll(p, &end, 10);
    if (end == p) pya_die("int() could not parse this string");
    while (*end == ' ' || *end == '\t') end++;
    if (*end != '\0') pya_die("int() could not parse this string");
    return (int64_t)v;
}

static double pya_str_to_f(PStr *s) {
    /* Fast path (Clinger): [+-]digits[.digits], <= 15 significant digits.
     * mantissa and 10^frac are both exact doubles, so one IEEE division
     * gives the correctly-rounded result — bit-identical to strtod/Python. */
    static const double P10[16] = {1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7,
                                   1e8, 1e9, 1e10, 1e11, 1e12, 1e13, 1e14, 1e15};
    const char *p = s->data;
    int64_t n = s->len, i = 0;
    bool neg = false;
    if (n && (p[0] == '+' || p[0] == '-')) {
        neg = p[0] == '-';
        i = 1;
    }
    int64_t mant = 0, digits = 0, frac = 0;
    bool seen_dot = false, ok = i < n;
    for (; i < n && ok; i++) {
        char c = p[i];
        if (c >= '0' && c <= '9') {
            if (digits >= 15) { ok = false; break; }
            mant = mant * 10 + (c - '0');
            digits++;
            if (seen_dot) frac++;
        } else if (c == '.' && !seen_dot) {
            seen_dot = true;
        } else {
            ok = false;
        }
    }
    if (ok && digits > 0) {
        double v = (double)mant / P10[frac];
        return neg ? -v : v;
    }
    /* slow path: whitespace, exponents, long numbers, inf/nan, errors */
    char buf[64];
    const char *q = pya_numbuf(s, buf, sizeof buf);
    char *end = NULL;
    while (*q == ' ' || *q == '\t') q++;
    double v = strtod(q, &end);
    if (end == q) pya_die("float() could not parse this string");
    while (*end == ' ' || *end == '\t') end++;
    if (*end != '\0') pya_die("float() could not parse this string");
    return v;
}

/* ---------------- math with Python semantics ---------------- */

static int64_t pya_floordiv_i(int64_t a, int64_t b) {
    if (b == 0) pya_die("integer division by zero");
    int64_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) q--;
    return q;
}

static int64_t pya_mod_i(int64_t a, int64_t b) {
    if (b == 0) pya_die("integer modulo by zero");
    int64_t r = a % b;
    if (r != 0 && ((r < 0) != (b < 0))) r += b;
    return r;
}

static double pya_div_f(double a, double b) {
    if (b == 0.0) pya_die("division by zero");
    return a / b;
}

static double pya_mod_f(double a, double b) {
    if (b == 0.0) pya_die("float modulo by zero");
    double r = fmod(a, b);
    if (r != 0.0 && ((r < 0.0) != (b < 0.0))) r += b;
    return r;
}

static int64_t pya_pow_i(int64_t base, int64_t exp) {
    if (exp < 0) pya_die("negative exponent for int ** int; use floats");
    int64_t r = 1;
    while (exp > 0) {
        if (exp & 1) r *= base;
        base *= base;
        exp >>= 1;
    }
    return r;
}

static int64_t pya_abs_i(int64_t v) { return v < 0 ? -v : v; }
static int64_t pya_min_i(int64_t a, int64_t b) { return a < b ? a : b; }
static int64_t pya_max_i(int64_t a, int64_t b) { return a > b ? a : b; }
static double pya_min_f(double a, double b) { return a < b ? a : b; }
static double pya_max_f(double a, double b) { return a > b ? a : b; }

/* ---------------- str.split ---------------- */

static PList *pstr_split(PStr *s, PStr *sep) {
    if (sep->len == 0) pya_die("split() with an empty separator");
    PList *out = plist_new(16);
    int64_t start = 0;
    if (sep->len == 1) { /* memchr fast path for the common 1-char separator */
        const char c = sep->data[0];
        for (;;) {
            const char *hit = (const char *)memchr(s->data + start, c,
                                                   (size_t)(s->len - start));
            if (!hit) break;
            int64_t i = hit - s->data;
            plist_append(out, pval_p(pstr_view(s->data + start, i - start)));
            start = i + 1;
        }
    } else {
        int64_t i;
        while ((i = pstr_search(s, sep, start)) >= 0) {
            plist_append(out, pval_p(pstr_view(s->data + start, i - start)));
            start = i + sep->len;
        }
    }
    plist_append(out, pval_p(pstr_view(s->data + start, s->len - start)));
    return out;
}

/* ---------------- files ---------------- */

static PStr *pya_read_file(PStr *path) {
    const char *cpath = pya_cbuf(path);
    FILE *f = fopen(cpath, "rb");
    if (!f) {
        snprintf(pya_exc_buf, sizeof pya_exc_buf, "cannot open file: %s", cpath);
        pya_die(pya_exc_buf);
    }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (size < 0) size = 0;
    PStr *s = pstr_raw((int64_t)size);
    if (size && fread(s->data, 1, (size_t)size, f) != (size_t)size) {
        fclose(f);
        pya_die("failed to read file");
    }
    fclose(f);
    return s;
}

static PList *pya_read_lines(PStr *path) {
    PStr *all = pya_read_file(path);
    PList *out = plist_new(64);
    int64_t start = 0;
    while (start < all->len) { /* zero-copy: each line is a view into the file */
        const char *hit = (const char *)memchr(all->data + start, '\n',
                                               (size_t)(all->len - start));
        int64_t i = hit ? (hit - all->data) : all->len;
        int64_t end = i;
        if (end > start && all->data[end - 1] == '\r') end--;
        plist_append(out, pval_p(pstr_view(all->data + start, end - start)));
        start = i + 1;
    }
    return out;
}

static void pya_write_file(PStr *path, PStr *text) {
    const char *cpath = pya_cbuf(path);
    FILE *f = fopen(cpath, "wb");
    if (!f) {
        snprintf(pya_exc_buf, sizeof pya_exc_buf, "cannot write file: %s", cpath);
        pya_die(pya_exc_buf);
    }
    if (text->len && fwrite(text->data, 1, (size_t)text->len, f) != (size_t)text->len) {
        fclose(f);
        pya_die("failed to write file");
    }
    fclose(f);
}

/* ---------------- CLI support: args, input, exists, exit -------------- */

static int pya_g_argc = 0;
static char **pya_g_argv = NULL; /* UTF-8 */

static void pya_init_args(int argc, char **argv) {
#ifdef _WIN32
    /* take arguments from the wide command line so Unicode paths work */
    (void)argc; (void)argv;
    int wargc = 0;
    wchar_t **wargv = CommandLineToArgvW(GetCommandLineW(), &wargc);
    if (!wargv) return;
    pya_g_argv = (char **)malloc(sizeof(char *) * (size_t)wargc);
    if (!pya_g_argv) return;
    for (int i = 0; i < wargc; i++) {
        int n = WideCharToMultiByte(CP_UTF8, 0, wargv[i], -1, NULL, 0,
                                    NULL, NULL);
        pya_g_argv[i] = (char *)malloc((size_t)n);
        if (!pya_g_argv[i]) return;
        WideCharToMultiByte(CP_UTF8, 0, wargv[i], -1, pya_g_argv[i], n,
                            NULL, NULL);
    }
    pya_g_argc = wargc;
    LocalFree(wargv);
#else
    pya_g_argc = argc;
    pya_g_argv = argv;
#endif
}

/* the program's arguments (without the executable name) */
static PList *pya_args(void) {
    PList *l = plist_new(pya_g_argc > 1 ? pya_g_argc - 1 : 0);
    for (int i = 1; i < pya_g_argc; i++)
        plist_append(l, pval_p(pstr_from_c(pya_g_argv[i])));
    return l;
}

/* print the prompt, read one line from stdin; end-of-input is a catchable
 * runtime error (like Python's EOFError) */
static PStr *pya_input(PStr *prompt) {
    if (prompt->len) {
        fwrite(prompt->data, 1, (size_t)prompt->len, stdout);
        fflush(stdout);
    }
    int64_t cap = 128, n = 0;
    char *buf = (char *)pya_alloc((size_t)cap);
    for (;;) {
        int c = fgetc(stdin);
        if (c == EOF) {
            if (n == 0) pya_die("end of input");
            break;
        }
        if (c == '\n') break;
        if (n == cap) {
            char *nb = (char *)pya_alloc((size_t)cap * 2);
            memcpy(nb, buf, (size_t)n);
            buf = nb;
            cap *= 2;
        }
        buf[n++] = (char)c;
    }
    if (n > 0 && buf[n - 1] == '\r') n--;
    return pstr_new(buf, n);
}

static bool pya_exists(PStr *path) {
    FILE *f = fopen(pya_cbuf(path), "rb");
    if (f) {
        fclose(f);
        return true;
    }
    return false;
}

/* ---------------- clock ---------------- */

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
static double pya_clock(void) {
    LARGE_INTEGER freq, now;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&now);
    return (double)now.QuadPart / (double)freq.QuadPart;
}
#else
#include <time.h>
static double pya_clock(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}
#endif

/* ---------------- parallel for ----------------
 * Iterations are chunked across N OS threads (PYA_THREADS or core count).
 * The type checker guarantees bodies don't write shared state (except
 * distinct list slots), so workers only need thread-local allocation. GC is
 * deferred during a parallel region and resumes after the join.
 */

static int64_t pya_range_count(int64_t start, int64_t stop, int64_t step) {
    if (step == 0) pya_die("range() step cannot be 0");
    if (step > 0) return start >= stop ? 0 : (stop - start + step - 1) / step;
    return start <= stop ? 0 : (start - stop - step - 1) / (-step);
}

static int pya_nthreads(void) {
    static int cached = 0;
    if (cached) return cached;
    const char *e = getenv("PYA_THREADS");
    if (e) {
        int v = atoi(e);
        if (v >= 1) { cached = v > 128 ? 128 : v; return cached; }
    }
#ifdef _WIN32
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    cached = (int)si.dwNumberOfProcessors;
#else
    cached = (int)sysconf(_SC_NPROCESSORS_ONLN);
#endif
    if (cached < 1) cached = 1;
    if (cached > 128) cached = 128;
    return cached;
}

typedef struct {
    void (*fn)(void *);
    void *arg;
} PyaThreadStart;

#ifdef _WIN32
static unsigned __stdcall pya_thread_tramp(void *p) {
    PyaThreadStart *t = (PyaThreadStart *)p;
    t->fn(t->arg);
    return 0;
}
#else
static void *pya_thread_tramp(void *p) {
    PyaThreadStart *t = (PyaThreadStart *)p;
    t->fn(t->arg);
    return NULL;
}
#endif

static void pya_parallel_run(void (*fn)(void *), char *ctxs, size_t sz, int n) {
    if (n <= 1) {
        if (n == 1) fn(ctxs);
        return;
    }
    if (n > 128) n = 128;
    PyaThreadStart starts[128];
    pya_in_parallel = 1;
#ifdef _WIN32
    HANDLE handles[128];
    for (int i = 1; i < n; i++) {
        starts[i].fn = fn;
        starts[i].arg = ctxs + sz * (size_t)i;
        handles[i] = (HANDLE)_beginthreadex(NULL, 0, pya_thread_tramp,
                                            &starts[i], 0, NULL);
        if (!handles[i]) pya_die("failed to create worker thread");
    }
    fn(ctxs); /* the main thread works chunk 0 */
    for (int i = 1; i < n; i++) {
        WaitForSingleObject(handles[i], INFINITE);
        CloseHandle(handles[i]);
    }
#else
    pthread_t handles[128];
    for (int i = 1; i < n; i++) {
        starts[i].fn = fn;
        starts[i].arg = ctxs + sz * (size_t)i;
        if (pthread_create(&handles[i], NULL, pya_thread_tramp, &starts[i]))
            pya_die("failed to create worker thread");
    }
    fn(ctxs);
    for (int i = 1; i < n; i++) pthread_join(handles[i], NULL);
#endif
    pya_in_parallel = 0;
}

#endif /* PYA_RUNTIME_H */
