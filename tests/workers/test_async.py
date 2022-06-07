import asyncio
import sys
import time
import weakref
from random import random

import pytest

from hybrid_pool_executor.base import Action
from hybrid_pool_executor.constants import ACT_EXCEPTION, ACT_RESTART
from hybrid_pool_executor.workers.asyncio import (
    AsyncManager,
    AsyncManagerSpec,
    AsyncTask,
    AsyncWorker,
    AsyncWorkerSpec,
)


@pytest.mark.timeout(10)
def test_async_worker_task():
    async def simple_task():
        return "done"

    worker_spec = AsyncWorkerSpec(name="TestAsyncWorker", idle_timeout=1)
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()
    task = AsyncTask(name="simple_task", fn=simple_task)
    worker_spec.task_bus.put(task)

    worker = AsyncWorker(worker_spec)
    worker.start()

    assert task.future.result() == "done"

    worker.stop()
    assert not worker.is_alive()
    assert not worker.is_idle()

    ref = weakref.ref(worker)
    del worker_spec, worker, task
    assert ref() is None


@pytest.mark.timeout(10)
def test_sync_worker_task():
    def simple_task():
        return "done"

    worker_spec = AsyncWorkerSpec(name="TestAsyncWorker", idle_timeout=1)
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()
    task = AsyncTask(name="simple_task", fn=simple_task)
    worker_spec.task_bus.put(task)

    worker = AsyncWorker(worker_spec)
    worker.start()

    assert task.future.result() == "done"

    worker.stop()
    assert not worker.is_alive()
    assert not worker.is_idle()

    ref = weakref.ref(worker)
    del worker_spec, worker, task
    assert ref() is None


@pytest.mark.timeout(10)
@pytest.mark.asyncio
async def test_async_worker_task_async_future():
    async def simple_task():
        return "done"

    worker_spec = AsyncWorkerSpec(name="TestAsyncWorker", idle_timeout=1)
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()
    task = AsyncTask(name="simple_task", fn=simple_task)
    worker_spec.task_bus.put(task)

    worker = AsyncWorker(worker_spec)
    worker.start()

    assert await task.future == "done"
    assert task.future.result() == "done"
    assert await task.future == "done"

    worker.stop()


@pytest.mark.timeout(10)
def test_async_worker_error():
    async def simple_error_task():
        raise RuntimeError("error")

    worker_spec = AsyncWorkerSpec(
        name="TestAsyncWorker",
        idle_timeout=1,
        max_err_count=1,
    )
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()
    task = AsyncTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)

    worker = AsyncWorker(worker_spec)
    worker.start()

    with pytest.raises(RuntimeError, match="error"):
        _ = task.future.result()

    response: Action = worker_spec.response_bus.get()
    assert response.match(ACT_EXCEPTION)
    assert response.match(ACT_RESTART)

    worker.stop()
    assert not worker.is_alive()
    assert not worker.is_idle()

    ref = weakref.ref(worker)
    del worker_spec, worker, task

    # it costs more time to clean up on Windows/MacOS
    if sys.platform != "linux":
        time.sleep(1)

    assert ref() is None


@pytest.mark.timeout(10)
def test_async_worker_max_error():
    async def simple_task():
        return "done"

    async def simple_error_task():
        raise RuntimeError("error")

    worker_spec = AsyncWorkerSpec(
        name="TestAsyncWorker",
        idle_timeout=1,
        max_err_count=2,
        max_cons_err_count=-1,
    )
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()

    worker = AsyncWorker(worker_spec)
    worker.start()

    task = AsyncTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)
    with pytest.raises(RuntimeError, match="error"):
        _ = task.future.result()

    assert worker.is_alive()

    task = AsyncTask(name="simple_task", fn=simple_task)
    worker_spec.task_bus.put(task)
    _ = task.future.result()

    assert worker.is_alive()

    task = AsyncTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)
    with pytest.raises(RuntimeError, match="error"):
        _ = task.future.result()

    # wait for worker shutdown
    time.sleep(0.25)
    assert not worker.is_alive()


@pytest.mark.timeout(10)
def test_async_worker_cons_error():
    async def simple_error_task():
        raise RuntimeError("error")

    worker_spec = AsyncWorkerSpec(
        name="TestAsyncWorker",
        idle_timeout=1,
        max_err_count=-1,
        max_cons_err_count=2,
    )
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()

    worker = AsyncWorker(worker_spec)
    worker.start()

    task = AsyncTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)
    with pytest.raises(RuntimeError, match="error"):
        _ = task.future.result()

    assert worker.is_alive()

    task = AsyncTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)
    with pytest.raises(RuntimeError, match="error"):
        _ = task.future.result()

    # wait for worker shutdown
    time.sleep(0.25)
    assert not worker.is_alive()


@pytest.mark.timeout(10)
def test_async_manager():
    async def simple_task():
        return "done"

    manager_spec = AsyncManagerSpec()
    manager = AsyncManager(manager_spec)
    manager.start()

    future = manager.submit(simple_task)
    assert future.result() == "done"

    manager.stop()


@pytest.mark.timeout(10)
@pytest.mark.asyncio
async def test_async_manager_high_concurrency():
    async def simple_task(v):
        await asyncio.sleep(random())
        return v

    with AsyncManager(AsyncManagerSpec()) as manager:
        futures = []
        for i in range(1024):
            futures.append(manager.submit(simple_task, (i,)))
        for i, future in enumerate(futures):
            assert await future == i
