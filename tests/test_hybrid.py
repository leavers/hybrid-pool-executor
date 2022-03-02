import asyncio
import pytest
import time
from random import random
from hybrid_pool_executor import HybridPoolExecutor


def simple_task():
    return "done"


def simple_delay_task(v):
    time.sleep(random())
    return v


async def simple_async_delay_task(v):
    await asyncio.sleep(random())
    return v


def test_executor_simple():
    pool = HybridPoolExecutor()
    future = pool.submit(simple_task)
    assert future.result() == "done"


@pytest.mark.asyncio
async def test_executor_high_concurrency():
    futures = {
        "thread": [],
        "process": [],
        "async": [],
    }
    with HybridPoolExecutor() as pool:
        for i in range(1024):
            if i < 32:
                futures["process"].append(
                    pool.submit_task(fn=simple_delay_task, args=(i,), mode="process")
                )
            futures["thread"].append(
                pool.submit_task(fn=simple_delay_task, args=(i,), mode="thread")
            )
            futures["async"].append(
                pool.submit_task(fn=simple_async_delay_task, args=(i,), mode="async")
            )
            # TODO: process futures need syncing among processes by manager, code will
            #       be probably blocked here to wait for all process futures to be set.
    for i in range(1024):
        if i < 32:
            assert await futures["process"][i] == i
        assert await futures["thread"][i] == i
        assert await futures["async"][i] == i


@pytest.mark.asyncio
async def test_executor_run_in_executor():
    pool = HybridPoolExecutor()
    loop = asyncio.get_event_loop()
    assert await loop.run_in_executor(pool, simple_task) == "done"
