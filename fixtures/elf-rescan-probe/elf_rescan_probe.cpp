#include "native/alloc/elf_import_hooks.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <time.h>
#include <vector>

#include <dlfcn.h>

namespace {

std::uint64_t clockNs(clockid_t id) noexcept
{
    timespec ts{};
    if (::clock_gettime(id, &ts) != 0) {
        return 0;
    }
    return static_cast<std::uint64_t>(ts.tv_sec) * 1000000000ULL + static_cast<std::uint64_t>(ts.tv_nsec);
}

void probeReplacement() noexcept {}

double percentile(std::vector<std::uint64_t> values, double fraction)
{
    if (values.empty()) {
        return 0.0;
    }
    std::sort(values.begin(), values.end());
    const std::size_t index = static_cast<std::size_t>((values.size() - 1) * fraction);
    return static_cast<double>(values[index]) / 1000000.0;
}

}  // namespace

extern "C" const char *sparkElfRescanProbeRun(unsigned warmups, unsigned iterations)
{
    static thread_local char output[8192]{};
    output[0] = '\0';

    if (iterations == 0 || iterations > 1000 || warmups > 1000) {
        std::snprintf(output, sizeof(output), "{\"status\":\"FAIL\",\"error\":\"invalid iteration count\"}");
        return output;
    }

    const std::array specs{
        spark::ElfImportHookSpec{.name = "malloc", .replacement = reinterpret_cast<void *>(&probeReplacement), .required = true},
        spark::ElfImportHookSpec{.name = "calloc", .replacement = reinterpret_cast<void *>(&probeReplacement), .required = true},
        spark::ElfImportHookSpec{.name = "realloc", .replacement = reinterpret_cast<void *>(&probeReplacement), .required = true},
        spark::ElfImportHookSpec{.name = "free", .replacement = reinterpret_cast<void *>(&probeReplacement), .required = true},
        spark::ElfImportHookSpec{.name = "reallocarray", .replacement = reinterpret_cast<void *>(&probeReplacement), .required = false},
        spark::ElfImportHookSpec{.name = "aligned_alloc", .replacement = reinterpret_cast<void *>(&probeReplacement), .required = false},
        spark::ElfImportHookSpec{.name = "posix_memalign", .replacement = reinterpret_cast<void *>(&probeReplacement), .required = false},
    };

    spark::ElfImportHooks hooks;
    std::string error;
    if (!hooks.prepare(specs, error)) {
        std::snprintf(output, sizeof(output), "{\"status\":\"FAIL\",\"stage\":\"prepare\",\"error\":\"%.1024s\"}", error.c_str());
        return output;
    }

    for (unsigned i = 0; i < warmups; ++i) {
        if (!hooks.rescan(error)) {
            std::snprintf(output, sizeof(output), "{\"status\":\"FAIL\",\"stage\":\"warmup\",\"error\":\"%.1024s\"}", error.c_str());
            return output;
        }
    }

    std::vector<std::uint64_t> wall;
    std::vector<std::uint64_t> cpu;
    wall.reserve(iterations);
    cpu.reserve(iterations);
    for (unsigned i = 0; i < iterations; ++i) {
        const std::uint64_t cpu_begin = clockNs(CLOCK_THREAD_CPUTIME_ID);
        const std::uint64_t wall_begin = clockNs(CLOCK_MONOTONIC);
        if (!hooks.rescan(error)) {
            std::snprintf(output, sizeof(output), "{\"status\":\"FAIL\",\"stage\":\"measure\",\"iteration\":%u,\"error\":\"%.1024s\"}", i, error.c_str());
            return output;
        }
        const std::uint64_t wall_end = clockNs(CLOCK_MONOTONIC);
        const std::uint64_t cpu_end = clockNs(CLOCK_THREAD_CPUTIME_ID);
        wall.push_back(wall_end - wall_begin);
        cpu.push_back(cpu_end - cpu_begin);
    }

    std::uint64_t wall_sum = 0;
    std::uint64_t cpu_sum = 0;
    for (auto value : wall) wall_sum += value;
    for (auto value : cpu) cpu_sum += value;
    const double wall_mean_ms = static_cast<double>(wall_sum) / static_cast<double>(iterations) / 1000000.0;
    const double cpu_mean_ms = static_cast<double>(cpu_sum) / static_cast<double>(iterations) / 1000000.0;
    const double amortized_cpu_percent = cpu_mean_ms / 5000.0 * 100.0;

    std::snprintf(
        output,
        sizeof(output),
        "{\"status\":\"PASS\",\"measurement\":\"production-ElfImportHooks-scan-path-in-real-BDS-process\","
        "\"installed_patch_phase_included\":false,\"warmups\":%u,\"iterations\":%u,"
        "\"target_count\":%zu,\"page_count\":%zu,\"hooked_module_count\":%zu,\"skipped_module_count\":%zu,\"failed_module_count\":%zu,"
        "\"wall_mean_ms\":%.6f,\"wall_p50_ms\":%.6f,\"wall_p95_ms\":%.6f,\"wall_max_ms\":%.6f,"
        "\"thread_cpu_mean_ms\":%.6f,\"thread_cpu_p50_ms\":%.6f,\"thread_cpu_p95_ms\":%.6f,\"thread_cpu_max_ms\":%.6f,"
        "\"period_ms\":5000,\"scan_only_amortized_cpu_percent\":%.9f}",
        warmups,
        iterations,
        hooks.targetCount(), hooks.pageCount(), hooks.hookedModuleCount(), hooks.skippedModuleCount(), hooks.failedModuleCount(),
        wall_mean_ms, percentile(wall, 0.50), percentile(wall, 0.95), percentile(wall, 1.0),
        cpu_mean_ms, percentile(cpu, 0.50), percentile(cpu, 0.95), percentile(cpu, 1.0),
        amortized_cpu_percent);
    return output;
}
