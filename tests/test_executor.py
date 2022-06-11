import asyncio
import sys
import time
from random import random

import pytest

from hybrid_pool_executor import HybridPoolExecutor


def simple_task():
    return "done"


def simple_delay_task(v):
    time.sleep(random())
    return v


async def simple_async_delay_task(v):
    await asyncio.sleep(random())
    return v


@pytest.mark.timeout(10)
def test_executor_simple():
    pool = HybridPoolExecutor()
    future = pool.submit(simple_task)
    assert future.result() == "done"


@pytest.mark.timeout(10)
def test_executor_map():
    pool = HybridPoolExecutor()
    results = pool.map(simple_delay_task, range(4))
    for i, result in enumerate(results):
        assert result == i


@pytest.mark.timeout(10)
def test_executor_map_tasks():
    pool = HybridPoolExecutor()
    results = pool.map(simple_delay_task, [(i, i) for i in range(4)])
    for i, result in enumerate(results):
        assert result == (i, i)


def test_executor_guess_mode():
    pool = HybridPoolExecutor()
    guess_mode = pool._guess_mode

    assert "thread" in guess_mode(simple_delay_task)
    assert "async" in guess_mode(simple_async_delay_task)
    assert guess_mode(simple_async_delay_task, mode="async") == {"async"}
    assert guess_mode(simple_delay_task, mode="thread") == {"thread"}
    assert guess_mode(simple_delay_task, mode="process") == {"process"}
    assert len(guess_mode(simple_delay_task, mode="async")) == 0

    assert "thread" in guess_mode(simple_async_delay_task, tags=["thread"])
    assert "process" in guess_mode(simple_async_delay_task, tags=["thread"])
    assert "thread" in guess_mode(simple_delay_task, tags=["thread"])
    assert "process" in guess_mode(simple_delay_task, tags=["thread"])
    assert "thread" not in guess_mode(simple_delay_task, tags=["process"])
    assert "process" in guess_mode(simple_delay_task, tags=["process"])

    assert len(guess_mode(simple_delay_task, tags=["black hole"])) == 0


@pytest.mark.skipif(sys.platform == "darwin", reason="test unstable on darwin")
@pytest.mark.timeout(20 if sys.platform == "linux" else 60)
@pytest.mark.asyncio
async def test_executor_high_concurrency():
    futures = {
        "thread": [],
        "process": [],
        "async": [],
    }
    total_tasks, process_tasks = (64, 16) if sys.platform == "linux" else (16, 4)
    # NOTE: The more threads are created, the more time they takes to stop, and the
    # longer the pytest timeout tolerance is required.
    with HybridPoolExecutor() as pool:
        for i in range(total_tasks):
            if i < process_tasks:
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
    for i in range(total_tasks):
        if i < process_tasks:
            assert await futures["process"][i] == i
        assert await futures["thread"][i] == i
        assert await futures["async"][i] == i


@pytest.mark.timeout(10)
@pytest.mark.asyncio
async def test_executor_run_in_executor():
    pool = HybridPoolExecutor()
    loop = asyncio.get_event_loop()
    assert await loop.run_in_executor(pool, simple_task) == "done"
