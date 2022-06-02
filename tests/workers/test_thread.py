import asyncio
import time
import weakref
from random import random

import pytest

from hybrid_pool_executor.base import Action
from hybrid_pool_executor.constants import ACT_EXCEPTION, ACT_RESTART
from hybrid_pool_executor.workers.thread import (
    ThreadManager,
    ThreadManagerSpec,
    ThreadTask,
    ThreadWorker,
    ThreadWorkerSpec,
)


@pytest.mark.timeout(10)
def test_thread_worker_task():
    def simple_task():
        return "done"

    worker_spec = ThreadWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=1,
    )
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()
    task = ThreadTask(name="simple_task", fn=simple_task)
    worker_spec.task_bus.put(task)

    worker = ThreadWorker(worker_spec)
    worker.start()

    assert task.future.result() == "done"

    worker.stop()
    assert not worker.is_alive()
    assert not worker.is_idle()

    ref = weakref.ref(worker)
    del worker_spec, worker, task
    assert ref() is None


@pytest.mark.timeout(10)
def test_thread_worker_async_task():
    async def simple_task(v):
        await asyncio.sleep(0.1)
        return v

    worker_spec = ThreadWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=1,
    )
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()
    tasks = []
    for i in range(3):
        task = ThreadTask(name="simple_task", fn=simple_task, args=[i])
        tasks.append(task)
        worker_spec.task_bus.put(task)

    worker = ThreadWorker(worker_spec)
    worker.start()

    for i in range(3):
        assert tasks[i].future.result() == i

    worker.stop()
    assert not worker.is_alive()
    assert not worker.is_idle()

    ref = weakref.ref(worker)
    del worker_spec, worker, task
    assert ref() is None


@pytest.mark.timeout(10)
def test_thread_worker_error():
    def simple_error_task():
        raise RuntimeError("error")

    worker_spec = ThreadWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=1,
    )
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()
    task = ThreadTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)

    worker = ThreadWorker(worker_spec)
    worker.start()

    with pytest.raises(RuntimeError, match="error"):
        _ = task.future.result()

    response: Action = worker_spec.response_bus.get()
    assert response.match(ACT_EXCEPTION)
    assert response.match(ACT_RESTART)

    worker.stop()
    assert not worker.is_alive()
    assert not worker.is_idle()


@pytest.mark.timeout(10)
def test_thread_worker_max_error():
    def simple_task():
        return "done"

    def simple_error_task():
        raise RuntimeError("error")

    worker_spec = ThreadWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=2,
        max_cons_err_count=-1,
    )
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()

    worker = ThreadWorker(worker_spec)
    worker.start()

    task = ThreadTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)
    with pytest.raises(RuntimeError, match="error"):
        _ = task.future.result()

    assert worker.is_alive()

    task = ThreadTask(name="simple_task", fn=simple_task)
    worker_spec.task_bus.put(task)
    _ = task.future.result()

    assert worker.is_alive()

    task = ThreadTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)
    with pytest.raises(RuntimeError, match="error"):
        _ = task.future.result()

    # wait for worker shutting down
    time.sleep(0.25)

    assert not worker.is_alive()


@pytest.mark.timeout(10)
def test_thread_worker_cons_error():
    def simple_error_task():
        raise RuntimeError("error")

    worker_spec = ThreadWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=-1,
        max_cons_err_count=2,
    )
    worker_spec.task_bus = worker_spec.task_bus_type()
    worker_spec.request_bus = worker_spec.request_bus_type()
    worker_spec.response_bus = worker_spec.response_bus_type()

    worker = ThreadWorker(worker_spec)
    worker.start()

    task = ThreadTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)
    with pytest.raises(RuntimeError, match="error"):
        _ = task.future.result()

    assert worker.is_alive()

    task = ThreadTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)
    with pytest.raises(RuntimeError, match="error"):
        _ = task.future.result()

    # wait for worker shutting down
    time.sleep(0.25)

    assert not worker.is_alive()


@pytest.mark.timeout(10)
def test_thread_manager():
    def simple_task():
        return "done"

    manager_spec = ThreadManagerSpec()
    manager = ThreadManager(manager_spec)
    manager.start()

    future = manager.submit(simple_task)
    assert future.result() == "done"

    manager.stop()


@pytest.mark.timeout(10)
@pytest.mark.asyncio
async def test_thread_manager_high_concurrency():
    def simple_task(v):
        time.sleep(random())
        return v

    # NOTE: The more threads are created, the more time they takes to stop, and the
    # longer the pytest timeout tolerance is required.
    with ThreadManager(ThreadManagerSpec()) as manager:
        futures = []
        for i in range(64):
            futures.append(manager.submit(simple_task, (i,)))
        for i, future in enumerate(futures):
            assert await future == i
