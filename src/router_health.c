/*
 * router_health.c — minimal READ-ONLY health reporter for Asuswrt-style Linux routers.
 *
 * Design constraints (deliberate):
 *   - Read-only. It only reads /proc and calls statvfs(). No writes, no network,
 *     no sockets, no NVRAM access, no configuration dumps, no credentials.
 *   - On-demand, single-shot, prints one JSON document to stdout and exits.
 *   - Static-link friendly so it does not depend on the target's glibc version.
 *   - Every allocation and every output buffer is bounded; the JSON document is
 *     always well-formed, including when input is truncated or unavailable.
 *   - "Unavailable" is always distinguishable from "zero": a metric that could not
 *     be read is emitted as null (or an explicit error object), never as 0.
 *
 * Schema history:
 *   router_health/1 — had a "usb_storage" array that matched virtual placeholder
 *                     mounts and could contain duplicate records.
 *   router_health/2 — replaces it with a "storage" object whose "candidates" are
 *                     block-device-backed mounts only, de-duplicated, plus a count
 *                     of excluded placeholders. Adds a machine-readable "units" block.
 *
 * ABI this source was built against (observed in the RT-AX56U rootfs):
 *   ELF32, little-endian, ARM (EM_ARM), EABI5, SOFT-FLOAT (e_flags 0x05000200).
 * Matching those flags is necessary but NOT proof of runtime compatibility — the
 * reporter was separately executed on the unit under a bounded, approved test.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <signal.h>
#include <sys/statvfs.h>
#include <sys/utsname.h>
#include <math.h>

#define TOOL_NAME    "router_health"
#define TOOL_VERSION "1.0.4"
#define SCHEMA       "router_health/2"

/* Hard bounds. Each exists so that a pathological /proc file cannot make the tool
   allocate without limit or emit a truncated (malformed) JSON document.
   TRUNCATION IS ALWAYS REPORTED: the document carries a "limits" block naming the
   bound, how many mount records were SEEN, how many were skipped as overlong and
   how many files were read partially, so a silently short answer cannot be mistaken
   for a complete one. */
#define SLURP_MAX      (4u * 1024u * 1024u)   /* max bytes read from any one file   */
#define CPUSTAT_MAX    (64u * 1024u)          /* max serialised cpu_stat body size  */
#define STATVFS_MAX_ENTRIES 512               /* max mounts considered              */

/* Counts of files whose read hit SLURP_MAX. Reported in "limits". */
static int g_files_truncated = 0;

/* Test hook: --proc-root DIR re-points every /proc read at DIR. Read-only, no
   behaviour change when unset. Used by the fixture tests. */
static char g_proc[512] = "/proc";

static const char *pf(const char *name)
{
    static char b[640];
    snprintf(b, sizeof b, "%s/%s", g_proc, name);
    return b;
}

/* ---------------------------------------------------------------- helpers */

/* `truncated` (may be NULL) is set to 1 when the file was larger than SLURP_MAX and
   the read stopped at the bound. */
static char *slurp(const char *path, int *truncated)
{
    if (truncated) *truncated = 0;
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    size_t cap = 8192, len = 0;
    char *buf = malloc(cap);
    if (!buf) { fclose(f); return NULL; }
    size_t n;
    while ((n = fread(buf + len, 1, cap - len - 1, f)) > 0) {
        len += n;
        if (len + 1 >= cap) {
            if (cap >= SLURP_MAX) {                      /* bounded: stop growing */
                if (truncated) *truncated = 1;
                g_files_truncated++;
                break;
            }
            size_t ncap = cap * 2;
            if (ncap > SLURP_MAX) ncap = SLURP_MAX;
            char *nb = realloc(buf, ncap);
            if (!nb) { free(buf); fclose(f); return NULL; }
            buf = nb; cap = ncap;
        }
    }
    buf[len] = '\0';
    fclose(f);
    return buf;
}

/* Length of a valid UTF-8 sequence starting at lead byte c, or 0 if c cannot
   start one. */
static int utf8_seq_len(unsigned char c)
{
    if (c < 0x80) return 1;
    if (c >= 0xC2 && c <= 0xDF) return 2;
    if (c >= 0xE0 && c <= 0xEF) return 3;
    if (c >= 0xF0 && c <= 0xF4) return 4;
    return 0;
}

/* True when the n bytes at src are a well-formed UTF-8 sequence (continuation
   bytes in range, and rejecting overlong forms, UTF-16 surrogates and code
   points above U+10FFFF). */
static int utf8_valid_at(const char *src, int n)
{
    const unsigned char *s = (const unsigned char *)src;
    unsigned char c = s[0];
    for (int k = 1; k < n; k++)
        if (s[k] < 0x80 || s[k] > 0xBF) return 0;
    if (n == 3) {
        if (c == 0xE0 && s[1] < 0xA0) return 0;      /* overlong */
        if (c == 0xED && s[1] > 0x9F) return 0;      /* surrogate */
    } else if (n == 4) {
        if (c == 0xF0 && s[1] < 0x90) return 0;      /* overlong */
        if (c == 0xF4 && s[1] > 0x8F) return 0;      /* above U+10FFFF */
    }
    return 1;
}

/* Copy a JSON-safe version of src into dst (bounded, always NUL-terminated).

   ENCODING POLICY: the emitted text is guaranteed to be valid UTF-8. Bytes that
   form a well-formed UTF-8 sequence are copied through unchanged; any byte that
   cannot be decoded (an invalid lead or continuation byte, or a multibyte
   sequence the buffer bound would cut) is replaced with U+FFFD REPLACEMENT
   CHARACTER, emitted as the JSON escape \ufffd. This is a documented
   lossy-replacement policy: a filesystem name containing invalid bytes still
   yields valid JSON, at the cost of not being byte-for-byte reversible. */
static void json_escape(const char *src, char *dst, size_t dstsz)
{
    size_t j = 0;
    if (dstsz == 0) return;
    for (size_t i = 0; src && src[i]; ) {
        unsigned char c = (unsigned char)src[i];
        if (c < 0x20 || c == '"' || c == '\\') {
            char esc[8];
            if (c == '"')       snprintf(esc, sizeof esc, "\\\"");
            else if (c == '\\') snprintf(esc, sizeof esc, "\\\\");
            else if (c == '\n') snprintf(esc, sizeof esc, "\\n");
            else if (c == '\r') snprintf(esc, sizeof esc, "\\r");
            else if (c == '\t') snprintf(esc, sizeof esc, "\\t");
            else                snprintf(esc, sizeof esc, "\\u%04x", c);
            size_t el = strlen(esc);
            if (j + el >= dstsz) break;
            memcpy(dst + j, esc, el);
            j += el;
            i++;
            continue;
        }
        if (c < 0x80) {
            if (j + 1 >= dstsz) break;
            dst[j++] = (char)c;
            i++;
            continue;
        }
        int n = utf8_seq_len(c);
        if (n == 0 || !utf8_valid_at(src + i, n)) {
            const char *rep = "\\ufffd";
            size_t el = strlen(rep);
            if (j + el >= dstsz) break;
            memcpy(dst + j, rep, el);
            j += el;
            i++;
            continue;
        }
        if (j + (size_t)n >= dstsz) break;   /* would not fit: stop cleanly */
        memcpy(dst + j, src + i, (size_t)n);
        j += (size_t)n;
        i += (size_t)n;
    }
    dst[j] = '\0';
}
/* Decode the octal escapes /proc/mounts uses (space→\040, tab→\011, nl→\012, \→\134).

   ONLY values 1..255 are decoded. A `\000` would embed a NUL and silently truncate
   the rest of the path; a value above `\377` is not a single byte at all and would
   wrap when cast to char. Both are left as their literal text, so the path stays
   intact and inspectable instead of being silently mangled. */
static void mount_unescape(char *s)
{
    char *r = s, *w = s;
    while (*r) {
        if (r[0] == '\\' && r[1] >= '0' && r[1] <= '7' &&
            r[2] >= '0' && r[2] <= '7' && r[3] >= '0' && r[3] <= '7') {
            int v = (r[1]-'0')*64 + (r[2]-'0')*8 + (r[3]-'0');
            if (v >= 1 && v <= 255) {
                *w++ = (char)v;
                r += 4;
                continue;
            }
        }
        *w++ = *r++;
    }
    *w = '\0';
}

static char *trim(char *s)
{
    while (*s == ' ' || *s == '\t') s++;
    char *e = s + strlen(s);
    while (e > s && (e[-1] == '\n' || e[-1] == '\r' || e[-1] == ' ' || e[-1] == '\t')) *--e = '\0';
    return s;
}

/* ------------------------------------------------------------- /proc/stat */

static int cpustat_append(char *body, size_t cap, size_t *len, int first,
                          const char *key,
                          unsigned long long u, unsigned long long n, unsigned long long s,
                          unsigned long long id, unsigned long long io, unsigned long long ir,
                          unsigned long long si, unsigned long long st)
{
    if (*len >= cap) return 0;
    int w = snprintf(body + *len, cap - *len,
                     "%s\"%s\":{\"user\":%llu,\"nice\":%llu,\"system\":%llu,"
                     "\"idle\":%llu,\"iowait\":%llu,\"irq\":%llu,\"softirq\":%llu,\"steal\":%llu}",
                     first ? "" : ",", key, u,n,s,id,io,ir,si,st);
    if (w < 0 || (size_t)w >= cap - *len) {
        body[*len] = '\0';   /* discard any partial text snprintf wrote */
        return 0;
    }
    *len += (size_t)w;
    return 1;
}

/* Per-CPU and aggregate jiffies counters. Cumulative, USER_HZ (typically 100 Hz);
   utilisation requires two samples. null when /proc/stat is unavailable. */
static void emit_cpu_stat(void)
{
    int itrunc = 0;
    char *buf = slurp(pf("stat"), &itrunc);
    if (!buf) { printf("\"cpu_stat\":null"); return; }
    char *body = malloc(CPUSTAT_MAX);
    if (!body) { free(buf); printf("\"cpu_stat\":null"); return; }
    body[0] = '\0';
    size_t len = 0;
    int first = 1, truncated = 0;
    char *line = strtok(buf, "\n");
    while (line) {
        if (strncmp(line, "cpu", 3) == 0 && (line[3] == ' ' || (line[3] >= '0' && line[3] <= '9'))) {
            char key[32];
            unsigned long long u=0,n=0,s=0,id=0,io=0,ir=0,si=0,st=0;
            if (sscanf(line, "%31s %llu %llu %llu %llu %llu %llu %llu %llu",
                       key, &u,&n,&s,&id,&io,&ir,&si,&st) >= 5) {
                if (!cpustat_append(body, CPUSTAT_MAX, &len, first, key, u,n,s,id,io,ir,si,st)) {
                    truncated = 1;
                    break;
                }
                first = 0;
            }
        }
        line = strtok(NULL, "\n");
    }
    if (truncated)           printf("\"cpu_stat\":{\"_truncated\":true%s%s}", len ? "," : "", body);
    else if (first)          printf("\"cpu_stat\":null");
    else                     printf("\"cpu_stat\":{%s}", body);
    free(body);
    free(buf);
}

/* ------------------------------------------------------------- /proc/loadavg */

static void emit_loadavg(void)
{
    int itrunc = 0;
    char *buf = slurp(pf("loadavg"), &itrunc);
    if (!buf) { printf("\"loadavg\":null"); return; }
    char *t = trim(buf);
    double l1=0,l5=0,l15=0; unsigned running=0,total=0; unsigned long lastpid=0;
    if (sscanf(t, "%lf %lf %lf %u/%u %lu", &l1,&l5,&l15,&running,&total,&lastpid) >= 5
        && isfinite(l1) && isfinite(l5) && isfinite(l15))
        printf("\"loadavg\":{\"load1\":%.2f,\"load5\":%.2f,\"load15\":%.2f,"
               "\"procs_running\":%u,\"procs_total\":%u}", l1,l5,l15,running,total);
    else
        printf("\"loadavg\":null");
    free(buf);
}

/* ------------------------------------------------------------- /proc/cpuinfo */

static void emit_cpuinfo(void)
{
    int itrunc = 0;
    char *buf = slurp(pf("cpuinfo"), &itrunc);
    if (!buf) { printf("\"cpu\":null"); return; }
    int count = 0;
    char model[128] = "", hardware[128] = "", mhz[64] = "";
    char *save, *line = strtok_r(buf, "\n", &save);
    while (line) {
        char *c = strchr(line, ':');
        if (c) {
            *c = '\0';
            char *k = trim(line), *v = trim(c + 1);
            if (strncmp(k, "processor", 9) == 0) count++;
            else if (!model[0]    && !strcmp(k, "model name")) strncpy(model, v, sizeof(model)-1);
            else if (!model[0]    && !strcmp(k, "Processor"))  strncpy(model, v, sizeof(model)-1);
            else if (!hardware[0] && !strcmp(k, "Hardware"))   strncpy(hardware, v, sizeof(hardware)-1);
            else if (!mhz[0]      && !strcmp(k, "BogoMIPS"))   snprintf(mhz, sizeof(mhz), "%s", v);
        }
        line = strtok_r(NULL, "\n", &save);
    }
    /* A /proc/cpuinfo that yielded NO processor entry and no model is not a CPU
       description at all (an unreadable or unparseable file): report it as
       UNAVAILABLE, never as a CPU count of zero. */
    if (count == 0 && !model[0] && !hardware[0] && !mhz[0]) {
        printf("\"cpu\":null");
        free(buf);
        return;
    }
    char em[800], eh[800], emhz[512];
    json_escape(model, em, sizeof(em));
    json_escape(hardware, eh, sizeof(eh));
    /* BogoMIPS is device-controlled text too: it must be escaped like every other
       string, or a value containing a quote or a backslash produces malformed JSON
       and the collector rejects the whole sample. */
    json_escape(mhz, emhz, sizeof(emhz));
    printf("\"cpu\":{\"count\":%d,\"model\":\"%s\",\"hardware\":\"%s\",\"bogomips\":\"%s\"}",
           count, em, eh, emhz);
    free(buf);
}

/* ------------------------------------------------------------- /proc/meminfo */

/* Values are in kB as the kernel reports them (Linux "kB" = 1024 bytes).
   Each known key is always emitted: a value the kernel did not supply is null,
   so "not reported" is never confused with a genuine zero. */
static void emit_meminfo(void)
{
    static const char *want[] = {
        "MemTotal","MemFree","MemAvailable","Buffers","Cached","SwapTotal","SwapFree", NULL
    };
    int found[8] = {0,0,0,0,0,0,0,0};
    unsigned long long val[8] = {0,0,0,0,0,0,0,0};
    int itrunc = 0;
    char *buf = slurp(pf("meminfo"), &itrunc);
    if (buf) {
        char *save, *line = strtok_r(buf, "\n", &save);
        while (line) {
            for (int i = 0; want[i]; i++) {
                size_t kl = strlen(want[i]);
                if (!found[i] && strncmp(line, want[i], kl) == 0 && line[kl] == ':') {
                    if (sscanf(line + kl + 1, " %llu", &val[i]) == 1) found[i] = 1;
                    break;
                }
            }
            line = strtok_r(NULL, "\n", &save);
        }
    }
    printf("\"memory_kb\":{");
    for (int i = 0; want[i]; i++) {
        printf("%s\"%s\":", i ? "," : "", want[i]);
        if (found[i]) printf("%llu", val[i]);
        else          printf("null");        /* kernel did not report it: UNKNOWN, not 0 */
    }
    printf("}");
    if (buf) free(buf);
}

/* ------------------------------------------------------------------ statvfs */

/* df-compatible Use%, rounded UP to match df (which uses a ceiling, not a floor):
     pct = ceil(used * 100 / (used + avail))
   The product is guarded against overflow so a very large filesystem cannot wrap
   the multiplication. */
static unsigned used_percent_ceiling(unsigned long long used,
                                     unsigned long long avail)
{
    unsigned long long denom = used + avail;
    if (denom == 0) return 0;
    unsigned long long pct;
    if (used > (0xFFFFFFFFFFFFFFFFULL - denom) / 100ULL) {
        pct = (unsigned long long)((double)used * 100.0 / (double)denom + 0.9999);
    } else {
        pct = (used * 100ULL + denom - 1ULL) / denom;
    }
    if (pct > 100ULL) pct = 100ULL;
    return (unsigned)pct;
}

/* Emits df-compatible capacity figures:
     total_bytes = f_blocks  * f_frsize
     free_bytes  = f_bfree   * f_frsize   (filesystem-wide free, incl. reserved)
     avail_bytes = f_bavail  * f_frsize   (available to an unprivileged writer)
     used_bytes  = total - free
     used_percent = ceil(used / (used + avail))   [same basis and rounding as df]
   inodes are null when the kernel reports none (e.g. UBIFS), not 0.

   A filesystem can report MORE free blocks than total blocks (a shrinking mount, or
   a filesystem bug). `total - f_bfree` then UNDERFLOWS to a near-2^64 value, making
   used_percent absurd and making the collector reject the whole sample, so the free
   figures are clamped to total first. */
static void emit_statvfs_pair(const char *path)
{
    struct statvfs v;
    char ep[512];
    json_escape(path, ep, sizeof(ep));
    if (statvfs(path, &v) != 0) {
        char err[128];
        json_escape(strerror(errno), err, sizeof(err));
        printf("{\"path\":\"%s\",\"available\":false,\"error\":\"%s\"}", ep, err);
        return;
    }
    unsigned long long bsize = v.f_frsize ? v.f_frsize : v.f_bsize;
    unsigned long long total = (unsigned long long)v.f_blocks * bsize;
    unsigned long long avail = (unsigned long long)v.f_bavail * bsize;
    unsigned long long free_ = (unsigned long long)v.f_bfree  * bsize;
    if (free_ > total) free_ = total;
    if (avail > total) avail = total;
    unsigned long long used  = total - free_;
    unsigned pct = used_percent_ceiling(used, avail);
    printf("{\"path\":\"%s\",\"available\":true,\"total_bytes\":%llu,\"free_bytes\":%llu,"
           "\"avail_bytes\":%llu,\"used_bytes\":%llu,\"used_percent\":%u,",
           ep, total, free_, avail, used, pct);
    if (v.f_files == 0 && v.f_ffree == 0)
        printf("\"inodes\":null,\"inodes_free\":null}");
    else
        printf("\"inodes\":%llu,\"inodes_free\":%llu}",
               (unsigned long long)v.f_files, (unsigned long long)v.f_ffree);
}

/* --------------------------------------------------------------- /proc/mounts */

struct mnt { char dev[256], mp[256], fstype[64], opts[256]; };

/* Split one /proc/mounts line into its first four whitespace-separated fields.

   Returns 1 on success, 0 when the line has fewer than four fields, and -1 when a
   field is TOO LONG for its buffer. The overlong case is reported, never truncated:
   `sscanf("%255s ...")` silently cut a long field and left the remainder in the
   stream, so every LATER field shifted by one and statvfs ran on a mangled path. */
static int split_mount_line(char *line, char *dev, size_t dn, char *mp, size_t mn,
                            char *fstype, size_t fn, char *opts, size_t on)
{
    char *dst[4];
    size_t cap[4];
    dst[0] = dev;    cap[0] = dn;
    dst[1] = mp;     cap[1] = mn;
    dst[2] = fstype; cap[2] = fn;
    dst[3] = opts;   cap[3] = on;

    char *p = line;
    for (int i = 0; i < 4; i++) {
        while (*p == ' ' || *p == '\t') p++;
        if (!*p || *p == '\n' || *p == '\r') return 0;      /* too few fields */
        char *start = p;
        while (*p && *p != ' ' && *p != '\t' && *p != '\n' && *p != '\r') p++;
        size_t len = (size_t)(p - start);
        if (len == 0 || len >= cap[i]) return -1;           /* overlong: do not cut */
        memcpy(dst[i], start, len);
        dst[i][len] = '\0';
    }
    return 1;
}

static int is_virtual_fs(const char *t)
{
    static const char *skip[] = {
        "proc","sysfs","devpts","mqueue","cgroup","cgroup2","debugfs","tracefs",
        "securityfs","binfmt_misc","fusectl","pstore","configfs","rpc_pipefs",
        "nsfs","none","autofs","ramfs","hugetlbfs","devtmpfs","tmpfs", NULL
    };
    for (int i = 0; skip[i]; i++) if (!strcmp(t, skip[i])) return 1;
    return 0;
}

static int is_block_device(const char *dev) { return strncmp(dev, "/dev/", 5) == 0; }

/* A storage CANDIDATE must be backed by a real block device and live under one of
   the storage mount roots. This excludes virtual placeholder mounts such as the
   tmpfs the firmware parks at /tmp/mnt before any disk is attached.
   NOTE: "block-backed" is NOT "USB" — see the note emitted with the array. */
static int is_storage_candidate(const struct mnt *m)
{
    if (!is_block_device(m->dev))       return 0;
    if (is_virtual_fs(m->fstype))       return 0;
    if (strncmp(m->mp, "/tmp/mnt", 8) == 0)      return 1;
    if (strncmp(m->mp, "/mnt/", 5) == 0)         return 1;
    if (strncmp(m->dev, "/dev/sd", 7) == 0)      return 1;
    if (strncmp(m->dev, "/dev/mmcblk", 11) == 0) return 1;
    if (strncmp(m->dev, "/dev/disk", 9) == 0)    return 1;
    return 0;
}

/* True if `e` duplicates an entry already in arr[0..n-1]. */
static int mount_is_dup(const struct mnt *arr, int n, const struct mnt *e)
{
    for (int i = 0; i < n; i++)
        if (!strcmp(arr[i].dev, e->dev) && !strcmp(arr[i].mp, e->mp) &&
            !strcmp(arr[i].fstype, e->fstype))
            return 1;
    return 0;
}

/* Parse /proc/mounts, de-duplicating identical (dev, mountpoint, fstype) records.
   The ASUS firmware lists /tmp/mnt twice, which previously produced duplicate
   filesystem and storage entries. Returns the count of kept records.

   `seen`, `overlong`, `dups_dropped` and `truncated` (all optional) report what the
   bound did, so a short list is never mistaken for a complete one. */
static int parse_mounts(struct mnt **out, int *dups_dropped, int *seen,
                        int *overlong, int *truncated)
{
    *out = NULL;
    if (dups_dropped) *dups_dropped = 0;
    if (seen) *seen = 0;
    if (overlong) *overlong = 0;
    if (truncated) *truncated = 0;
    int itrunc = 0;
    char *buf = slurp(pf("mounts"), &itrunc);
    if (!buf) return 0;
    if (itrunc && truncated) *truncated = 1;
    int cap = 32, n = 0;
    struct mnt *m = malloc(sizeof(*m) * cap);
    char *save, *line = strtok_r(buf, "\n", &save);
    while (line && m) {
        struct mnt e;
        int r = split_mount_line(line, e.dev, sizeof e.dev, e.mp, sizeof e.mp,
                                 e.fstype, sizeof e.fstype, e.opts, sizeof e.opts);
        if (r > 0) {
            if (seen) (*seen)++;
            mount_unescape(e.dev);
            mount_unescape(e.mp);
            if (mount_is_dup(m, n, &e)) {
                if (dups_dropped) (*dups_dropped)++;
                line = strtok_r(NULL, "\n", &save);
                continue;
            }
            if (n == cap) {
                if (cap >= STATVFS_MAX_ENTRIES) {
                    if (truncated) *truncated = 1;
                    break;
                }
                int ncap = cap * 2;
                if (ncap > STATVFS_MAX_ENTRIES) ncap = STATVFS_MAX_ENTRIES;
                struct mnt *nm = realloc(m, sizeof(*m) * ncap);
                if (!nm) break;
                m = nm; cap = ncap;
            }
            m[n++] = e;
        } else if (r < 0) {
            if (seen) (*seen)++;
            if (overlong) (*overlong)++;
        }
        line = strtok_r(NULL, "\n", &save);
    }
    free(buf);
    *out = m;
    return n;
}

/* ------------------------------------------------------------------ selftest */

/* Read the EXPLICIT boot identity from /proc/stat's `btime <epoch>` line.
   Returns 1 and sets *out on success; 0 when the file or the line is unavailable or
   out of range. `btime` is a wall-clock epoch, so it is only ever COMPARED between
   two samples, never used as a duration. */
static int read_btime(long long *out)
{
    int itrunc = 0;
    char *buf = slurp(pf("stat"), &itrunc);
    if (!buf) return 0;
    int found = 0;
    char *save, *line = strtok_r(buf, "\n", &save);
    while (line) {
        if (strncmp(line, "btime ", 6) == 0) {
            long long v = 0;
            if (sscanf(line + 6, "%lld", &v) == 1 && v > 0 && v < 1000000000000LL) {
                *out = v;
                found = 1;
            }
            break;
        }
        line = strtok_r(NULL, "\n", &save);
    }
    free(buf);
    return found;
}

static int selftest(void)
{
    int fail = 0;
    char small[8], out[512];

    /* 1. octal unescaping */
    char p1[] = "/tmp/mnt/My\\040Disk";
    mount_unescape(p1);
    if (strcmp(p1, "/tmp/mnt/My Disk")) { fprintf(stderr, "FAIL unescape space\n"); fail++; }
    char p2[] = "/tmp/mnt/a\\011b\\012c\\134d";
    mount_unescape(p2);
    if (strcmp(p2, "/tmp/mnt/a\tb\nc\\d")) { fprintf(stderr, "FAIL unescape tab/nl/bs\n"); fail++; }
    char p3[] = "/tmp/mnt/plain";
    mount_unescape(p3);
    if (strcmp(p3, "/tmp/mnt/plain")) { fprintf(stderr, "FAIL unescape no-op\n"); fail++; }

    /* 2. json_escape */
    json_escape("a\"b\\c\nd", out, sizeof(out));
    if (strcmp(out, "a\\\"b\\\\c\\nd")) { fprintf(stderr, "FAIL escape\n"); fail++; }
    memset(small, 0x7f, sizeof(small));
    json_escape("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", small, sizeof(small));
    if (strlen(small) > sizeof(small) - 1) { fprintf(stderr, "FAIL escape bounds len\n"); fail++; }
    if (small[strlen(small)] != '\0') { fprintf(stderr, "FAIL escape terminator\n"); fail++; }

    /* 3. cpu_stat truncation guard */
    for (int iter = 0; iter < 2; iter++) {
        size_t cap = (size_t)(iter == 0 ? CPUSTAT_MAX : 200);
        char *body = malloc(cap); size_t len = 0; int first = 1, added = 0, truncated = 0;
        body[0] = '\0';
        for (;;) {
            if (!cpustat_append(body, cap, &len, first, "cpu", 1,2,3,4,5,6,7,8)) { truncated = 1; break; }
            first = 0; added++;
            if (added > 100000) break;
        }
        if (added == 0 || len >= cap) { fprintf(stderr, "FAIL cpustat cap\n"); fail++; }
        if (len == 0 || body[len-1] != '}') { fprintf(stderr, "FAIL cpustat close\n"); fail++; }
        int depth = 0, bal = 1;
        for (size_t i = 0; i < len; i++) {
            if (body[i] == '{') depth++;
            else if (body[i] == '}') { depth--; if (depth < 0) bal = 0; }
        }
        if (!bal || depth != 0) { fprintf(stderr, "FAIL cpustat braces\n"); fail++; }
        if (cap == 200 && !truncated) { fprintf(stderr, "FAIL cpustat should truncate\n"); fail++; }
        free(body);
    }

    /* 4. mount de-duplication */
    struct mnt a[4];
    memset(a, 0, sizeof a);
    strcpy(a[0].dev, "tmpfs");     strcpy(a[0].mp, "/tmp/mnt");     strcpy(a[0].fstype, "tmpfs");
    strcpy(a[1].dev, "/dev/sda1"); strcpy(a[1].mp, "/tmp/mnt/USB"); strcpy(a[1].fstype, "vfat");
    struct mnt d = a[0];
    if (!mount_is_dup(a, 2, &d))    { fprintf(stderr, "FAIL dup not detected\n"); fail++; }
    struct mnt nd = a[1];
    strcpy(nd.mp, "/tmp/mnt/other");
    if (mount_is_dup(a, 2, &nd))    { fprintf(stderr, "FAIL false dup\n"); fail++; }

    /* 5. storage-candidate filtering: placeholders out, block-backed in */
    struct mnt ph; memset(&ph, 0, sizeof ph);
    strcpy(ph.dev, "tmpfs"); strcpy(ph.mp, "/tmp/mnt"); strcpy(ph.fstype, "tmpfs");
    if (is_storage_candidate(&ph))  { fprintf(stderr, "FAIL tmpfs placeholder accepted\n"); fail++; }
    struct mnt bk; memset(&bk, 0, sizeof bk);
    strcpy(bk.dev, "/dev/sda1"); strcpy(bk.mp, "/tmp/mnt/USB"); strcpy(bk.fstype, "vfat");
    if (!is_storage_candidate(&bk)) { fprintf(stderr, "FAIL block-backed rejected\n"); fail++; }
    struct mnt nv; memset(&nv, 0, sizeof nv);
    strcpy(nv.dev, "proc"); strcpy(nv.mp, "/proc"); strcpy(nv.fstype, "proc");
    if (is_storage_candidate(&nv))  { fprintf(stderr, "FAIL proc accepted\n"); fail++; }
    struct mnt root; memset(&root, 0, sizeof root);
    strcpy(root.dev, "/dev/root"); strcpy(root.mp, "/"); strcpy(root.fstype, "ext4");
    if (is_storage_candidate(&root)){ fprintf(stderr, "FAIL rootfs accepted as storage\n"); fail++; }

    /* 6. octal escapes that are NOT a single byte must be left literal.
          \000 would embed a NUL and truncate the path; \777 is above one byte. */
    char q1[] = "/tmp/mnt/a\\000b";
    mount_unescape(q1);
    if (strcmp(q1, "/tmp/mnt/a\\000b")) { fprintf(stderr, "FAIL \\000 decoded\n"); fail++; }
    char q2[] = "/tmp/mnt/a\\777b";
    mount_unescape(q2);
    if (strcmp(q2, "/tmp/mnt/a\\777b")) { fprintf(stderr, "FAIL \\777 decoded\n"); fail++; }
    char q3[] = "/tmp/mnt/a\\377b";                 /* 255 IS one byte */
    mount_unescape(q3);
    if ((unsigned char)q3[10] != 0xFF) { fprintf(stderr, "FAIL \\377 not decoded\n"); fail++; }

    /* 7. /proc/mounts splitting: a normal line, a short line, and an OVERLONG field
          that must be refused (not truncated, which would shift later fields). */
    {
        char l1[] = "/dev/sda1 /tmp/mnt/USB vfat rw,relatime 0 0";
        char d[64], m2[64], f[64], o[64];
        if (split_mount_line(l1, d, sizeof d, m2, sizeof m2, f, sizeof f, o, sizeof o) != 1
            || strcmp(d, "/dev/sda1") || strcmp(m2, "/tmp/mnt/USB") || strcmp(f, "vfat"))
            { fprintf(stderr, "FAIL mount split\n"); fail++; }
        char l2[] = "/dev/sda1 /mnt";
        if (split_mount_line(l2, d, sizeof d, m2, sizeof m2, f, sizeof f, o, sizeof o) != 0)
            { fprintf(stderr, "FAIL short mount line accepted\n"); fail++; }
        /* device name longer than the destination buffer */
        static char l3[600];
        memset(l3, 0, sizeof l3);
        snprintf(l3, sizeof l3, "/dev/%0200d /tmp/mnt/USB vfat rw 0 0", 1);
        if (split_mount_line(l3, d, 32, m2, sizeof m2, f, sizeof f, o, sizeof o) != -1)
            { fprintf(stderr, "FAIL overlong mount field accepted\n"); fail++; }
    }

    /* 8. statvfs percentage arithmetic: df-style ceiling, clamped to 100, and safe
          on the overflow path for a very large filesystem. */
    if (used_percent_ceiling(0, 100) != 0)     { fprintf(stderr, "FAIL pct zero\n"); fail++; }
    if (used_percent_ceiling(1, 2) != 34)      { fprintf(stderr, "FAIL pct ceil (floor would be 33)\n"); fail++; }
    if (used_percent_ceiling(1, 99) != 1)      { fprintf(stderr, "FAIL pct one\n"); fail++; }
    if (used_percent_ceiling(100, 0) != 100)   { fprintf(stderr, "FAIL pct full\n"); fail++; }
    if (used_percent_ceiling(9223372036854775808ULL, 1) != 100)
        { fprintf(stderr, "FAIL pct overflow path\n"); fail++; }

    if (fail == 0) printf("selftest: OK (8 groups)\n");
    else           printf("selftest: %d FAILURE(S)\n", fail);
    return fail;
}

/* -------------------------------------------------------------------- main */

int main(int argc, char **argv)
{
    /* A closed stdout must become a REPORTED write failure with a non-zero exit, not
       a silent signal death that a status-only reader could misread. */
    signal(SIGPIPE, SIG_IGN);

    int pretty = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--pretty") || !strcmp(argv[i], "-p")) pretty = 1;
        else if (!strcmp(argv[i], "--selftest")) return selftest();
        else if (!strcmp(argv[i], "--proc-root") && i + 1 < argc) {
            snprintf(g_proc, sizeof g_proc, "%s", argv[++i]);
            size_t L = strlen(g_proc);
            while (L > 1 && g_proc[L-1] == '/') g_proc[--L] = '\0';
        }
    }

    const char *nl = pretty ? "\n" : "";
    const char *sp = pretty ? " " : "";

    struct utsname un;
    if (uname(&un) != 0) { strcpy(un.sysname, "?"); strcpy(un.release, "?"); strcpy(un.machine, "?"); }

    /* uptime: a genuine zero is preserved as 0.0; missing OR malformed input is null.
       "unavailable" must never be reported as 0. */
    int have_up = 0;
    double up = 0.0;
    char *uptime = slurp(pf("uptime"), NULL);
    if (uptime) {
        double v;
        if (sscanf(uptime, "%lf", &v) == 1 && v >= 0.0 && v < 1e12) { up = v; have_up = 1; }
        free(uptime);
    }

    char esys[128], erel[128], emach[128];
    json_escape(un.sysname, esys, sizeof(esys));
    json_escape(un.release, erel, sizeof(erel));
    json_escape(un.machine, emach, sizeof(emach));

    struct mnt *mnts = NULL;
    int dups = 0, mnts_seen = 0, mnts_overlong = 0, mnts_truncated = 0;
    int nmnts = parse_mounts(&mnts, &dups, &mnts_seen, &mnts_overlong, &mnts_truncated);

    /* boot identity: /proc/stat's `btime` is the EXPLICIT instant the kernel booted.
       Comparing it between two samples detects a reboot without relying on uptime
       moving backwards or on a wall clock. This is a second small read of /proc/stat;
       the file is a few kilobytes and is read again by emit_cpu_stat. */
    int have_bt = 0;
    long long btime = 0;
    if (!read_btime(&btime)) { have_bt = 0; } else { have_bt = 1; }

    printf("{%s", nl);
    printf("\"schema\":\"%s\",%s", SCHEMA, nl);
    printf("\"tool\":\"%s\",\"version\":\"%s\",%s", TOOL_NAME, TOOL_VERSION, nl);
    printf("\"units\":{\"uptime_seconds\":\"s\",\"load*\":\"1-min/5-min/15-min runnable average\","
           "\"memory_kb\":\"kB (Linux kB = 1024 bytes)\","
           "\"cpu_stat\":\"USER_HZ jiffies, cumulative counters (needs 2 samples)\","
           "\"boot_time\":\"epoch seconds, from /proc/stat btime (boot identity)\","
           "\"capacity\":\"bytes\",\"used_percent\":\"ceil(used/(used+avail)), df-compatible\"},%s", nl);
    printf("\"host\":{\"sysname\":\"%s\",\"release\":\"%s\",\"machine\":\"%s\",\"uptime_seconds\":",
           esys, erel, emach);
    if (have_up) printf("%.1f", up); else printf("null");
    printf(",\"boot_time\":");
    if (have_bt) printf("%lld", btime); else printf("null");
    printf("},%s", nl);

    /* Explicit reporting of every hard bound, so a shorter-than-expected document is
       distinguishable from a complete one rather than being silently trusted. */
    printf("\"limits\":{\"file_read_max_bytes\":%u,\"files_truncated\":%d,"
           "\"mounts_max\":%d,\"mounts_seen\":%d,\"mounts_overlong\":%d,"
           "\"mounts_truncated\":%s},%s",
           (unsigned)SLURP_MAX, g_files_truncated, STATVFS_MAX_ENTRIES, mnts_seen,
           mnts_overlong, mnts_truncated ? "true" : "false", nl);

    fputs(sp, stdout); emit_cpuinfo();   printf(",%s", nl);
    fputs(sp, stdout); emit_loadavg();   printf(",%s", nl);
    fputs(sp, stdout); emit_meminfo();   printf(",%s", nl);
    fputs(sp, stdout); emit_cpu_stat();  printf(",%s", nl);

    printf("\"filesystems\":[%s", nl);
    int first = 1;
    for (int i = 0; i < nmnts; i++) {
        if (is_virtual_fs(mnts[i].fstype)) continue;
        printf("%s%s", first ? "" : (pretty ? ",\n " : ","), sp);
        emit_statvfs_pair(mnts[i].mp);
        first = 0;
    }
    printf("%s],%s", nl, nl);

    int ph_excluded = 0, cand_n = 0;
    for (int i = 0; i < nmnts; i++) {
        if (is_storage_candidate(&mnts[i])) cand_n++;
        else if (strncmp(mnts[i].mp, "/tmp/mnt", 8) == 0 || strncmp(mnts[i].mp, "/mnt/", 5) == 0)
            ph_excluded++;
    }

    printf("\"storage\":{%s", nl);
    printf("%s\"candidates\":[%s", sp, nl);
    first = 1;
    for (int i = 0; i < nmnts; i++) {
        if (!is_storage_candidate(&mnts[i])) continue;
        char ed[1600], em[1600], ef[400], eo[1600];
        json_escape(mnts[i].dev, ed, sizeof(ed));
        json_escape(mnts[i].mp,  em, sizeof(em));
        json_escape(mnts[i].fstype, ef, sizeof(ef));
        json_escape(mnts[i].opts, eo, sizeof(eo));
        printf("%s%s{\"device\":\"%s\",\"mountpoint\":\"%s\",\"fstype\":\"%s\","
               "\"options\":\"%s\",\"block_backed\":true,\"usb_identity\":\"not_established\","
               "\"capacity\":", first ? "" : (pretty ? ",\n " : ","), sp, ed, em, ef, eo);
        emit_statvfs_pair(mnts[i].mp);
        printf("}");
        first = 0;
    }
    printf("%s],%s", nl, nl);
    printf("%s\"candidates_count\":%d,%s", sp, cand_n, nl);
    printf("%s\"placeholders_excluded\":%d,%s", sp, ph_excluded, nl);
    printf("%s\"duplicate_records_dropped\":%d,%s", sp, dups, nl);
    printf("%s\"note\":\"candidates are block-device-backed mounts; block-backed does not establish USB identity\"%s",
           sp, nl);
    printf("}%s", nl);

    printf("}%s", nl);

    free(mnts);

    /* Exit non-zero when the document could not be WRITTEN. `printf` failing (a full
       disk, a closed pipe) previously still exited 0, so a consumer that only checks
       the status would treat a truncated or absent document as a good sample.
       SIGPIPE is ignored above so a closed pipe surfaces as a write error here
       instead of killing the process with a signal. */
    if (fflush(stdout) != 0 || ferror(stdout)) {
        fprintf(stderr, "router_health: stdout write failed: %s\n", strerror(errno));
        return 1;
    }
    return 0;
}
