// Only included by a separately compiled diagnostic candidate shim.
// No clocks, hooks, or altered source are installed in the scored executable.
#pragma once
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include "acl/acl.h"

#ifdef OJ_PDIST_SYNC_STATUS_ONLY
extern "C" uint64_t OJLaunchCount();
namespace oj_pdist_host {
// Only the first two calls of the one-case, one-buffer profile protocol are
// prevalidation. No clocks, addresses, strings from the runtime, or device data.
struct Counter { int kind; };
struct Allocation { uint64_t sequence, ordinal, launches; bool success; };
struct Sample {
    Counter alloc{0}, sync{1}, free{2};
    uint64_t sequence=0, alloc_calls=0, sync_calls=0, start=0, launches=0;
    int64_t code=0;
    uint64_t first_sequence=0, first_sync=0, first_alloc=0, first_launches=0;
    bool timeout_api=false, overflow=false;
    size_t subsequent_count=0;
    Allocation subsequent[64];
};
static Sample samples[2];
static size_t count=0;
static Sample* active=nullptr;
static void Begin() {
    if (active) { std::fprintf(stderr, "OJ_HOST_ERROR sample_reentry\n"); std::abort(); }
    if (count < 2) { active=&samples[count]; active->start=OJLaunchCount(); }
}
static void End() {
    if (active) { active->launches=OJLaunchCount()-active->start; active=nullptr; }
    ++count;
}
template<class F> static aclError Observe(Counter* counter, F call, bool timeout_api=false) {
    if (!counter) return call();
    Sample& s=*active;
    const uint64_t sequence=++s.sequence;
    if (counter->kind==0) ++s.alloc_calls;
    if (counter->kind==1) ++s.sync_calls;
    const uint64_t launches=OJLaunchCount()-s.start;
    const aclError code=call();
    if (counter->kind==1 && code!=ACL_SUCCESS && s.first_sequence==0) {
        s.code=code; s.first_sequence=sequence; s.first_sync=s.sync_calls;
        s.first_alloc=s.alloc_calls; s.first_launches=launches; s.timeout_api=timeout_api;
    } else if (counter->kind==0 && s.first_sequence) {
        if (s.subsequent_count==64) s.overflow=true;
        else s.subsequent[s.subsequent_count++]={sequence,s.alloc_calls,launches,code==ACL_SUCCESS};
    }
    return code;
}
struct Dump {
    ~Dump() {
        for (size_t i=0; i<count && i<2; ++i) {
            const auto& s=samples[i];
            std::fprintf(stderr, "OJ_SYNC {\"invocation\":%zu,\"api_calls\":%llu,\"sync_calls\":%llu,\"allocation_calls\":%llu,\"launch_calls\":%llu,\"overflow\":%s,\"first_non_success\":",
                i,(unsigned long long)s.sequence,(unsigned long long)s.sync_calls,
                (unsigned long long)s.alloc_calls,(unsigned long long)s.launches,s.overflow?"true":"false");
            if (!s.first_sequence) std::fprintf(stderr,"null");
            else std::fprintf(stderr,"{\"api\":\"%s\",\"code\":%lld,\"api_sequence\":%llu,\"sync_ordinal\":%llu,\"allocation_calls_before\":%llu,\"launch_calls_before\":%llu}",
                s.timeout_api?"aclrtSynchronizeStreamWithTimeout":"aclrtSynchronizeStream",
                (long long)s.code,(unsigned long long)s.first_sequence,(unsigned long long)s.first_sync,
                (unsigned long long)s.first_alloc,(unsigned long long)s.first_launches);
            std::fprintf(stderr,",\"subsequent_allocations\":[");
            for (size_t j=0;j<s.subsequent_count;++j) {
                const auto& a=s.subsequent[j];
                std::fprintf(stderr,"%s{\"api_sequence\":%llu,\"allocation_ordinal\":%llu,\"launch_calls_before\":%llu,\"success\":%s}",
                    j?",":"",(unsigned long long)a.sequence,(unsigned long long)a.ordinal,
                    (unsigned long long)a.launches,a.success?"true":"false");
            }
            std::fprintf(stderr,"]}\n");
        }
    }
};
static Dump dump;
}
#else
namespace oj_pdist_host {
using Clock = std::chrono::steady_clock;
struct Counter { uint64_t calls = 0, errors = 0; double us = 0; };
struct Sample { Counter alloc, sync, free; double host_us = 0; };
static Sample samples[1024];
static size_t count = 0;
static Sample* active = nullptr;
static Clock::time_point begin;
static double Micros(Clock::time_point start) {
    return std::chrono::duration<double, std::micro>(Clock::now() - start).count();
}
template<class F> static aclError Observe(Counter* counter, F call, bool = false) {
    if (!counter) return call();
    const auto start = Clock::now();
    const aclError result = call();
    const double us = Micros(start);
    ++counter->calls;
    counter->errors += result != ACL_SUCCESS;
    counter->us += us;
    return result;
}
static void Begin() {
    if (active || count == 1024) { std::fprintf(stderr, "OJ_HOST_ERROR sample_overflow_or_reentry\n"); std::abort(); }
    active = &samples[count];
    begin = Clock::now();
}
static void End() {
    active->host_us = Micros(begin);
    active = nullptr;
    ++count;
}
static void PrintCounter(const char* name, const Counter& c) {
    std::fprintf(stderr, ",\"%s\":{\"calls\":%llu,\"errors\":%llu,\"total_us\":%.9f}",
        name, (unsigned long long)c.calls, (unsigned long long)c.errors, c.us);
}
struct Dump {
    ~Dump() {
        // Defer formatting/I/O until all calls and correctness checks have ended.
        for (size_t i = 0; i < count; ++i) {
            std::fprintf(stderr, "OJ_HOST {\"invocation\":%zu,\"host_call_us\":%.9f", i, samples[i].host_us);
            PrintCounter("workspace_alloc", samples[i].alloc);
            PrintCounter("stream_sync", samples[i].sync);
            PrintCounter("workspace_free", samples[i].free);
            std::fprintf(stderr, "}\n");
        }
    }
};
static Dump dump;
}

#endif

static aclError oj_pdist_alloc(void** ptr, size_t bytes, aclrtMemMallocPolicy policy) {
    using namespace oj_pdist_host;
    return Observe(active ? &active->alloc : nullptr, [&] { return aclrtMalloc(ptr, bytes, policy); });
}
static aclError oj_pdist_free(void* ptr) {
    using namespace oj_pdist_host;
    return Observe(active ? &active->free : nullptr, [&] { return aclrtFree(ptr); });
}
static aclError oj_pdist_sync(aclrtStream stream) {
    using namespace oj_pdist_host;
    return Observe(active ? &active->sync : nullptr, [&] { return aclrtSynchronizeStream(stream); });
}
static aclError oj_pdist_sync_timeout(aclrtStream stream, int32_t timeout) {
    using namespace oj_pdist_host;
    return Observe(active ? &active->sync : nullptr, [&] { return aclrtSynchronizeStreamWithTimeout(stream, timeout); }, true);
}
