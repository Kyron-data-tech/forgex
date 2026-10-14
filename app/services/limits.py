import asyncio
import random
import time
from collections import deque
from pathlib import Path
from app.config import settings

class RateLimitExceeded(Exception):
    """Raised when job submission rate limit is reached."""
    pass

class RateLimiter:
    """Sliding-window rate limiter ensuring automation doesn't hammer services."""
    def __init__(self, max_per_hour: int = 30):
        self.max_per_hour = max_per_hour
        self.timestamps = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.time()
            one_hour_ago = now - 3600

            # Prune timestamps older than 1 hour
            while self.timestamps and self.timestamps[0] < one_hour_ago:
                self.timestamps.popleft()

            if len(self.timestamps) >= self.max_per_hour:
                oldest = self.timestamps[0]
                wait_seconds = int(3600 - (now - oldest)) + 1
                raise RateLimitExceeded(
                    f"Hourly job limit reached ({self.max_per_hour}/hour). Please wait {wait_seconds} seconds."
                )

            self.timestamps.append(now)

    def current_count(self) -> int:
        now = time.time()
        one_hour_ago = now - 3600
        while self.timestamps and self.timestamps[0] < one_hour_ago:
            self.timestamps.popleft()
        return len(self.timestamps)


async def human_delay(min_ms: int = 600, max_ms: int = 1800):
    """Introduces randomized human-like delay between browser automation steps."""
    delay = random.uniform(min_ms / 1000.0, max_ms / 1000.0)
    await asyncio.sleep(delay)


class GlobalJobLock:
    """Global semaphore guaranteeing strict concurrency of 1 for browser operations."""
    def __init__(self):
        self._sem = asyncio.Semaphore(1)

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._sem.release()

rate_limiter = RateLimiter(max_per_hour=settings.MAX_JOBS_PER_HOUR)
global_job_lock = GlobalJobLock()
