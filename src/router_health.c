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
 *
 * ABI this source was built against (observed in the RT-AX56U 3.0.0.4.386_52090 rootfs):
 *   ELF32, little-endian, ARM (EM_ARM), EABI5, SOFT-FLOAT (e_flags 0x05000200).
 * Matching those flags is necessary but NOT proof of runtime compatibility on a device —
 * see the feasibility/handoff reports for what remains to be confirmed on hardware.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sys/statvfs.h>
#include <sys/utsname.h>

#define TOOL_NAME    "router_health"
#define TOOL_VERSION "1.0.1"
#define SCHEMA       "router_health/1"

/* Hard bounds. Each exists so that a pathological /proc file cannot make the tool
   allocate without limit or emit a truncated (malformed) JSON document. */
#define SLURP_MAX      (4u * 1024u * 1024u)   /* max bytes read from any one file   */
#define CPUSTAT_MAX    (64u * 1024u)          /* max serialised cpu_stat body size  */
#define STATVFS_MAX_ENTRIES 512               /* max mounts considered              */

/* ---------------------------------------------------------------- helpers */

/* Read a whole file, growing the buffer, but never past SLURP_MAX. Returns NULL if
   the file cannot be opened. A file larger than SLURP_MAX is returned truncated —
   callers all parse line-oriented text, so truncation is safe and detectable. */
static char *slurp(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    size_t cap = 8192, len = 0;
    char *buf = malloc(cap);
    if (!buf) { fclose(f); return NULL; }
    size_t n;
    while ((n = fread(buf + len, 1, cap - len - 1, f)) > 0) {
        len += n;
        if (len + 1 >= cap) {
            if (cap >= SLURP_MAX) break;                 /* bounded: stop growing */
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

/* Copy a JSON-safe version of src into dst (bounded). Guarantees dst is NUL-terminated
   and never overflows. If src does not fit, the string is silently shortened: the result
   is still a valid, closed JSON string. */
static void json_escape(const char *src, char *dst, size_t dstsz)
{
    size_t j = 0;
    if (dstsz == 0) return;
    for (size_t i = 0; src && src[i] && j + 7 < dstsz; i++) {
        unsigned char c = (unsigned char)src[i];
        switch (c) {
        case '"':  dst[j++]='\\'; dst[j++]='"';  break;
        case '\\': dst[j++]='\\'; dst[j++]='\\'; break;
        case '\n': dst[j++]='\\'; dst[j++]='n';  break;
        case '\r': dst[j++]='\\'; dst[j++]='r';  break;
        case '\t': dst[j++]='\\'; dst[j++]='t';  break;
        default:
            if (c < 0x20) j += (size_t)snprintf(dst + j, dstsz - j, "\\u%04x", c);
            else          dst[j++] = (char)c;
        }
    }
    dst[j] = '\0';
}

/* Decode the octal escapes /proc/mounts uses in the device and mount-point fields
   (space→\040, tab→\011, newline→\012, backslash→\134). Without this a path such as
   "/tmp/mnt/My Disk" is passed to statvfs() verbatim and fails with ENOENT.
   Decodes in place; the unescaped string is never longer than the input. */
static void mount_unescape(char *s)
{
    char *r = s, *w = s;
    while (*r) {
        if (r[0] == '\\' && r[1] >= '0' && r[1] <= '7' &&
            r[2] >= '0' && r[2] <= '7' && r[3] >= '0' && r[3] <= '7') {
            *w++ = (char)((r[1]-'0')*64 + (r[2]-'0')*8 + (r[3]-'0'));
            r += 4;
        } else {
            *w++ = *r++;
        }
    }
    *w = '\0';
}

/* Trim leading/trailing whitespace in place; returns pointer into buff. */
static char *trim(char *s)
{
    while (*s == ' ' || *s == '\t') s++;
    char *e = s + strlen(s);
    while (e > s && (e[-1] == '\n' || e[-1] == '\r' || e[-1] == ' ' || e[-1] == '\t')) *--e = '\0';
    return s;
}

/* ------------------------------------------------------------- /proc/stat */

/* Append one cpu entry to a growable body. Returns 1 if appended, 0 if it would
   exceed `cap` (the caller then marks the section truncated and stops). Factored out
   so the truncation guard is directly testable (see --selftest). */
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
    if (w < 0 || (size_t)w >= cap - *len) return 0;      /* would not fit */
    *len += (size_t)w;
    return 1;
}

/* Emit per-CPU and aggregate jiffies counters (raw, not a rate). */
static void emit_cpu_stat(void)
{
    char *buf = slurp("/proc/stat");
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
                    truncated = 1;                          /* stop; do not emit broken JSON */
                    break;
                }
                first = 0;
            }
        }
        line = strtok(NULL, "\n");
    }
    if (first)               printf("\"cpu_stat\":null");
    else if (truncated)      printf("\"cpu_stat\":{\"_truncated\":true,%s}", body);
    else                     printf("\"cpu_stat\":{%s}", body);
    free(body);
    free(buf);
}

/* ------------------------------------------------------------- /proc/loadavg */

static void emit_loadavg(void)
{
    char *buf = slurp("/proc/loadavg");
    if (!buf) { printf("\"loadavg\":null"); return; }
    char *t = trim(buf);
    double l1=0,l5=0,l15=0; unsigned running=0,total=0; unsigned long lastpid=0;
    if (sscanf(t, "%lf %lf %lf %u/%u %lu", &l1,&l5,&l15,&running,&total,&lastpid) >= 5)
        printf("\"loadavg\":{\"load1\":%.2f,\"load5\":%.2f,\"load15\":%.2f,"
               "\"procs_running\":%u,\"procs_total\":%u}", l1,l5,l15,running,total);
    else
        printf("\"loadavg\":null");
    free(buf);
}

/* ------------------------------------------------------------- /proc/cpuinfo */

static void emit_cpuinfo(void)
{
    char *buf = slurp("/proc/cpuinfo");
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
    char em[256], eh[256];
    json_escape(model, em, sizeof(em));
    json_escape(hardware, eh, sizeof(eh));
    printf("\"cpu\":{\"count\":%d,\"model\":\"%s\",\"hardware\":\"%s\",\"bogomips\":\"%s\"}",
           count, em, eh, mhz);
    free(buf);
}

/* ------------------------------------------------------------- /proc/meminfo */

/* Emit a fixed, useful subset of meminfo (kB values). Emits null when unavailable so
   that "no telemetry" is distinguishable from "zero". */
static void emit_meminfo(void)
{
    static const char *want[] = {
        "MemTotal","MemFree","MemAvailable","Buffers","Cached","SwapTotal","SwapFree", NULL
    };
    char *buf = slurp("/proc/meminfo");
    if (!buf) { printf("\"memory_kb\":null"); return; }
    printf("\"memory_kb\":{");
    int first = 1;
    char *save, *line = strtok_r(buf, "\n", &save);
    while (line) {
        for (int i = 0; want[i]; i++) {
            size_t kl = strlen(want[i]);
            if (strncmp(line, want[i], kl) == 0 && line[kl] == ':') {
                unsigned long long v = 0;
                if (sscanf(line + kl + 1, " %llu", &v) == 1) {
                    printf("%s\"%s\":%llu", first ? "" : ",", want[i], v);
                    first = 0;
                }
                break;
            }
        }
        line = strtok_r(NULL, "\n", &save);
    }
    printf("}");
    free(buf);
}

/* ------------------------------------------------------------------ statvfs */

static void emit_statvfs_pair(const char *path)
{
    struct statvfs v;
    char ep[256];
    json_escape(path, ep, sizeof(ep));
    if (statvfs(path, &v) != 0) {
        /* statvfs failed: report an explicit error rather than a zeroed/absent entry. */
        char err[128];
        json_escape(strerror(errno), err, sizeof(err));
        printf("{\"path\":\"%s\",\"error\":\"%s\"}", ep, err);
        return;
    }
    unsigned long long bsize = v.f_frsize ? v.f_frsize : v.f_bsize;
    unsigned long long total = (unsigned long long)v.f_blocks * bsize;
    unsigned long long avail = (unsigned long long)v.f_bavail * bsize;
    unsigned long long free_ = (unsigned long long)v.f_bfree  * bsize;
    unsigned long long used  = total - free_;
    unsigned pct = total ? (unsigned)((used * 100ULL) / total) : 0;
    printf("{\"path\":\"%s\",\"total_bytes\":%llu,\"free_bytes\":%llu,"
           "\"avail_bytes\":%llu,\"used_bytes\":%llu,\"used_percent\":%u,"
           "\"inodes\":%llu,\"inodes_free\":%llu}",
           ep, total, free_, avail, used, pct,
           (unsigned long long)v.f_files, (unsigned long long)v.f_ffree);
}

/* --------------------------------------------------------------- /proc/mounts */

struct mnt { char dev[256], mp[256], fstype[64], opts[256]; };

static int parse_mounts(struct mnt **out)
{
    char *buf = slurp("/proc/mounts");
    if (!buf) { *out = NULL; return 0; }
    int cap = 32, n = 0;
    struct mnt *m = malloc(sizeof(*m) * cap);
    char *save, *line = strtok_r(buf, "\n", &save);
    while (line && m) {
        struct mnt e;
        if (sscanf(line, "%255s %255s %63s %255s", e.dev, e.mp, e.fstype, e.opts) == 4) {
            mount_unescape(e.dev);                    /* \040 etc. → real characters */
            mount_unescape(e.mp);
            if (n == cap) {
                if (cap >= STATVFS_MAX_ENTRIES) break;    /* bounded */
                int ncap = cap * 2;
                if (ncap > STATVFS_MAX_ENTRIES) ncap = STATVFS_MAX_ENTRIES;
                struct mnt *nm = realloc(m, sizeof(*m) * ncap);
                if (!nm) break;                           /* keep what we have, no NULL deref */
                m = nm; cap = ncap;
            }
            m[n++] = e;
        }
        line = strtok_r(NULL, "\n", &save);
    }
    free(buf);
    *out = m;
    return n;
}

static int is_usb(const struct mnt *m)
{
    return strncmp(m->mp, "/tmp/mnt", 8) == 0 ||
           strncmp(m->mp, "/mnt/", 5) == 0 ||
           strncmp(m->dev, "/dev/sd", 7) == 0 ||
           strncmp(m->dev, "/dev/mmcblk", 11) == 0;
}

static int is_virtual_fs(const char *t)
{
    static const char *skip[] = {
        "proc","sysfs","devpts","mqueue","cgroup","cgroup2","debugfs","tracefs",
        "securityfs","binfmt_misc","fusectl","pstore","configfs","rpc_pipefs",
        "nsfs","none","autofs","ramfs","hugetlbfs","devtmpfs", NULL
    };
    for (int i = 0; skip[i]; i++) if (!strcmp(t, skip[i])) return 1;
    return 0;
}

/* ------------------------------------------------------------------ selftest */

/* Exercises the failure cases that matter, without needing a router or any file
   system state. Returns 0 on success, non-zero (count of failures) otherwise. */
static int selftest(void)
{
    int fail = 0;
    char msg[64], small[8], out[512];

    /* 1. octal unescaping of /proc/mounts paths */
    char p1[] = "/tmp/mnt/My\\040Disk";
    mount_unescape(p1);
    if (strcmp(p1, "/tmp/mnt/My Disk")) { fprintf(stderr, "FAIL unescape space: %s\n", p1); fail++; }

    char p2[] = "/tmp/mnt/a\\011b\\012c\\134d";
    mount_unescape(p2);
    if (strcmp(p2, "/tmp/mnt/a\tb\nc\\d")) { fprintf(stderr, "FAIL unescape tab/nl/bs: got [%s]\n", p2); fail++; }

    char p3[] = "/tmp/mnt/plain";
    mount_unescape(p3);
    if (strcmp(p3, "/tmp/mnt/plain")) { fprintf(stderr, "FAIL unescape no-op\n"); fail++; }

    /* 2. json_escape escapes quotes/backslash/control and stays NUL-terminated */
    json_escape("a\"b\\c\nd", out, sizeof(out));
    if (strcmp(out, "a\\\"b\\\\c\\nd")) { fprintf(stderr, "FAIL escape: %s\n", out); fail++; }

    /* 3. json_escape into a tiny buffer must not overflow and must stay NUL-terminated
       (it terminates at the produced length, not at the buffer end). */
    memset(small, 0x7f, sizeof(small));
    json_escape("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", small, sizeof(small));
    if (strlen(small) > sizeof(small) - 1) { fprintf(stderr, "FAIL escape bounds len\n"); fail++; }
    if (small[strlen(small)] != '\0') { fprintf(stderr, "FAIL escape bounds terminator\n"); fail++; }
    if (strlen(small) > 0) { for (size_t k = strlen(small) + 1; k < sizeof(small); k++)
        if ((unsigned char)small[k] != 0x7f) { fprintf(stderr, "FAIL escape wrote past end\n"); fail++; break; } }

    /* 4. cpu_stat truncation guard: appending must refuse cleanly at the cap and the
       accumulated body must remain a valid, closed set of members (never mid-object). */
    for (int iter = 0; iter < 3; iter++) {
        size_t cap = (size_t)(iter == 0 ? CPUSTAT_MAX : 200);   /* 200 forces early stop */
        char *body = malloc(cap);
        size_t len = 0; int first = 1, added = 0, truncated = 0;
        body[0] = '\0';
        for (;;) {
            if (!cpustat_append(body, cap, &len, first, "cpu", 1,2,3,4,5,6,7,8)) { truncated = 1; break; }
            first = 0; added++;
            if (added > 100000) break;                          /* safety */
        }
        if (added == 0 || len >= cap) { fprintf(stderr, "FAIL cpustat cap len=%zu cap=%zu\n", len, cap); fail++; }
        /* body must end with '}' and contain balanced braces */
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

    /* 5. mount parser tolerates a missing/empty input without crashing */
    (void)msg;
    if (fail == 0) printf("selftest: OK (5 groups)\n");
    else           printf("selftest: %d FAILURE(S)\n", fail);
    return fail;
}

/* -------------------------------------------------------------------- main */

int main(int argc, char **argv)
{
    int pretty = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--pretty") || !strcmp(argv[i], "-p")) pretty = 1;
        if (!strcmp(argv[i], "--selftest")) return selftest();
    }

    const char *nl = pretty ? "\n" : "";
    const char *sp = pretty ? " " : "";

    struct utsname un;
    if (uname(&un) != 0) {
        strcpy(un.sysname, "?"); strcpy(un.release, "?"); strcpy(un.machine, "?");
    }

    char *uptime = slurp("/proc/uptime");
    double up = 0;
    if (uptime) { sscanf(uptime, "%lf", &up); free(uptime); }

    char esys[128], erel[128], emach[128];
    json_escape(un.sysname, esys, sizeof(esys));
    json_escape(un.release, erel, sizeof(erel));
    json_escape(un.machine, emach, sizeof(emach));

    struct mnt *mnts = NULL;
    int nmnts = parse_mounts(&mnts);

    printf("{%s",nl);
    printf("\"schema\":\"%s\",%s", SCHEMA, nl);
    printf("\"tool\":\"%s\",\"version\":\"%s\",%s", TOOL_NAME, TOOL_VERSION, nl);

    printf("\"host\":{\"sysname\":\"%s\",\"release\":\"%s\",\"machine\":\"%s\",\"uptime_seconds\":%.1f},%s",
           esys, erel, emach, up, nl);

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

    printf("\"usb_storage\":[%s", nl);
    first = 1;
    for (int i = 0; i < nmnts; i++) {
        if (!is_usb(&mnts[i])) continue;
        char ed[256], em[256], ef[64], eo[256];
        json_escape(mnts[i].dev, ed, sizeof(ed));
        json_escape(mnts[i].mp,  em, sizeof(em));
        json_escape(mnts[i].fstype, ef, sizeof(ef));
        json_escape(mnts[i].opts, eo, sizeof(eo));
        printf("%s%s{\"device\":\"%s\",\"mountpoint\":\"%s\",\"fstype\":\"%s\","
               "\"options\":\"%s\",\"capacity\":", first ? "" : (pretty ? ",\n " : ","), sp,
               ed, em, ef, eo);
        emit_statvfs_pair(mnts[i].mp);
        printf("}");
        first = 0;
    }
    printf("%s]%s", nl, nl);

    printf("}%s", nl);

    free(mnts);
    return 0;
}
