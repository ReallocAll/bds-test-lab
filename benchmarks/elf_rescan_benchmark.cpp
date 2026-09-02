#include "native/alloc/elf_import_hooks.h"

#include <dlfcn.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <span>
#include <string>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;

void fail(const std::string &message)
{
    std::cerr << message << '\n';
    std::exit(1);
}

double percentile(std::vector<double> values, double p)
{
    std::sort(values.begin(), values.end());
    if (values.empty()) {
        return 0.0;
    }
    const double position = p * static_cast<double>(values.size() - 1);
    const std::size_t lower = static_cast<std::size_t>(std::floor(position));
    const std::size_t upper = static_cast<std::size_t>(std::ceil(position));
    if (lower == upper) {
        return values[lower];
    }
    const double fraction = position - static_cast<double>(lower);
    return values[lower] * (1.0 - fraction) + values[upper] * fraction;
}

}  // namespace

int main(int argc, char **argv)
{
    std::vector<void *> handles;
    handles.reserve(argc > 1 ? static_cast<std::size_t>(argc - 1) : 0);
    for (int i = 1; i < argc; ++i) {
        void *handle = ::dlopen(argv[i], RTLD_NOW | RTLD_LOCAL);
        if (handle == nullptr) {
            const char *error = ::dlerror();
            fail(std::string("dlopen failed for ") + argv[i] + ": " + (error ? error : "unknown"));
        }
        handles.push_back(handle);
    }

    constexpr std::array<const char *, 7> names{
        "malloc", "calloc", "realloc", "free", "reallocarray", "aligned_alloc", "posix_memalign"};
    std::array<spark::ElfImportHookSpec, names.size()> specs{};
    for (std::size_t i = 0; i < names.size(); ++i) {
        ::dlerror();
        void *address = ::dlsym(RTLD_DEFAULT, names[i]);
        const char *error = ::dlerror();
        if (address == nullptr || error != nullptr) {
            fail(std::string("dlsym failed for ") + names[i] + ": " + (error ? error : "unknown"));
        }
        // Match Spark production: malloc/calloc/realloc/free are required while
        // reallocarray/aligned_alloc/posix_memalign are optional capabilities.
        // Point imports at their current libc implementation so the installed
        // scan+patch path is exercised without changing allocator semantics.
        specs[i] = spark::ElfImportHookSpec{.name = names[i], .replacement = address, .required = i < 4};
    }

    spark::ElfImportHooks hooks;
    std::string error;
    if (!hooks.prepare(std::span<const spark::ElfImportHookSpec>(specs), error)) {
        fail("prepare failed: " + error);
    }
    if (!hooks.install(error)) {
        fail("install failed: " + error);
    }

    constexpr int warmups = 20;
    constexpr int iterations = 250;
    for (int i = 0; i < warmups; ++i) {
        if (!hooks.rescan(error)) {
            fail("warmup rescan failed: " + error);
        }
    }

    std::vector<double> samples_us;
    samples_us.reserve(iterations);
    for (int i = 0; i < iterations; ++i) {
        const auto start = Clock::now();
        if (!hooks.rescan(error)) {
            fail("measured rescan failed: " + error);
        }
        const auto finish = Clock::now();
        samples_us.push_back(std::chrono::duration<double, std::micro>(finish - start).count());
    }

    const double total = std::accumulate(samples_us.begin(), samples_us.end(), 0.0);
    const double mean = total / static_cast<double>(samples_us.size());
    const auto [minimum, maximum] = std::minmax_element(samples_us.begin(), samples_us.end());
    const double amortized_us_per_second = mean / 5.0;
    const double equivalent_one_core_percent = amortized_us_per_second / 10000.0;

    std::cout << std::fixed << std::setprecision(3)
              << "{\n"
              << "  \"dummy_modules\": " << handles.size() << ",\n"
              << "  \"iterations\": " << iterations << ",\n"
              << "  \"hooked_modules\": " << hooks.hookedModuleCount() << ",\n"
              << "  \"skipped_modules\": " << hooks.skippedModuleCount() << ",\n"
              << "  \"failed_modules\": " << hooks.failedModuleCount() << ",\n"
              << "  \"targets\": " << hooks.targetCount() << ",\n"
              << "  \"pages\": " << hooks.pageCount() << ",\n"
              << "  \"rescan_us_min\": " << *minimum << ",\n"
              << "  \"rescan_us_mean\": " << mean << ",\n"
              << "  \"rescan_us_p50\": " << percentile(samples_us, 0.50) << ",\n"
              << "  \"rescan_us_p95\": " << percentile(samples_us, 0.95) << ",\n"
              << "  \"rescan_us_p99\": " << percentile(samples_us, 0.99) << ",\n"
              << "  \"rescan_us_max\": " << *maximum << ",\n"
              << "  \"period_seconds\": 5,\n"
              << "  \"amortized_us_per_second\": " << amortized_us_per_second << ",\n"
              << "  \"equivalent_one_core_percent\": " << equivalent_one_core_percent << "\n"
              << "}\n";

    if (!hooks.uninstall(error)) {
        fail("uninstall failed: " + error);
    }
    for (auto it = handles.rbegin(); it != handles.rend(); ++it) {
        ::dlclose(*it);
    }
    return 0;
}
