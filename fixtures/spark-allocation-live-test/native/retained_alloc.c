#include <stddef.h>

extern void *malloc(size_t);
extern void free(void *);
extern void *memset(void *, int, size_t);

#define SPARK_ALLOCATION_LIVE_MAX_BLOCKS 64U

static void *retained_blocks[SPARK_ALLOCATION_LIVE_MAX_BLOCKS];
static size_t retained_count;
static size_t retained_bytes;

static void release_retained(void)
{
    for (size_t index = 0; index < retained_count; ++index) {
        free(retained_blocks[index]);
        retained_blocks[index] = NULL;
    }
    retained_count = 0;
    retained_bytes = 0;
}

__attribute__((visibility("default"), noinline, used))
int spark_allocation_live_retain(size_t block_bytes, size_t block_count)
{
    if (block_bytes == 0 || block_count == 0 || block_count > SPARK_ALLOCATION_LIVE_MAX_BLOCKS || retained_count != 0) {
        return -1;
    }
    for (size_t index = 0; index < block_count; ++index) {
        void *block = malloc(block_bytes);
        if (block == NULL) {
            release_retained();
            return -1;
        }
        memset(block, 0xA5, block_bytes);
        retained_blocks[retained_count++] = block;
        retained_bytes += block_bytes;
    }
    return 0;
}

__attribute__((visibility("default"), noinline, used))
int spark_allocation_live_release(void)
{
    release_retained();
    return 0;
}

__attribute__((visibility("default"), noinline, used))
size_t spark_allocation_live_retained_blocks(void)
{
    return retained_count;
}

__attribute__((visibility("default"), noinline, used))
size_t spark_allocation_live_retained_bytes(void)
{
    return retained_bytes;
}
