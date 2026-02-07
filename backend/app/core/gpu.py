import asyncio
from contextlib import asynccontextmanager

# Global semaphore to limit concurrent GPU operations
# Default to 1 to prevent OOM
_gpu_semaphore = asyncio.Semaphore(1)

@asynccontextmanager
async def gpu_lock():
    """
    Async context manager to acquire GPU lock.
    Usage:
        async with gpu_lock():
            run_heavy_inference()
    """
    async with _gpu_semaphore:
        yield
