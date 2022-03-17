import asyncio
import sys
import time
import weakref
from random import random

import pytest

from hybrid_pool_executor.constants import ACT_EXCEPTION, ACT_RESTART
from hybrid_pool_executor.workers.process.worker import (
    Action,
    ProcessManager,
    ProcessManagerSpec,
    ProcessTask,
    ProcessWorker,
    ProcessWorkerSpec,
)


def simple_task():
    return "done"


def simple_error_task():
    raise RuntimeError("error")


def simple_task_v(v):
    time.sleep(random())
    return v


async def simple_async_task_v(v):
    await asyncio.sleep(random())
    return v


@pytest.mark.timeout(10)
def test_process_worker_task():
    worker_spec = ProcessWorkerSpec(
        name="TestProcessWorker",
        idle_timeout=1,
        max_err_count=1,
    )
    task = ProcessTask(name="simple_task", fn=simple_task)
    worker_spec.task_bus.put(task)

    worker = ProcessWorker(worker_spec)
    worker.start()

    response: Action = worker_spec.response_bus.get()
    assert response.result == "done"

    worker.stop()
    assert not worker.is_alive()
    assert not worker.is_idle()

    ref = weakref.ref(worker)
    del worker_spec, worker, task
    assert ref() is None


@pytest.mark.timeout(10)
def test_processs_worker_async_task():
    worker_spec = ProcessWorkerSpec(
        name="TestThreadWorker",
        max_task_count=3,
        idle_timeout=1,
        max_err_count=1,
    )
    tasks = []
    for i in range(3):
        task = ProcessTask(name="simple_async_task", fn=simple_async_task_v, args=[i])
        tasks.append(task)
        worker_spec.task_bus.put(task)

    worker = ProcessWorker(worker_spec)
    worker.start()

    for i in range(3):
        response: Action = worker_spec.response_bus.get()
        assert response.result == i

    worker.stop()
    assert not worker.is_alive()
    assert not worker.is_idle()

    ref = weakref.ref(worker)
    del worker_spec, worker, task
    assert ref() is None


@pytest.mark.timeout(10)
def test_process_worker_error():
    worker_spec = ProcessWorkerSpec(
        name="TestProcessWorker",
        idle_timeout=1,
        max_err_count=1,
    )
    task = ProcessTask(name="simple_error_task", fn=simple_error_task)
    worker_spec.task_bus.put(task)

    worker = ProcessWorker(worker_spec)
    worker.start()

    response: Action = worker_spec.response_bus.get()
    assert isinstance(response.exception, RuntimeError)
    assert response.match(ACT_EXCEPTION)
    assert response.match(ACT_RESTART)

    worker.stop()
    assert not worker.is_alive()
    assert not worker.is_idle()


@pytest.mark.timeout(10)
def test_process_manager():
    manager_spec = ProcessManagerSpec()
    manager = ProcessManager(manager_spec)
    manager.start()

    future = manager.submit(simple_task)
    assert future.result() == "done"

    manager.stop()


@pytest.mark.timeout(20 if sys.platform != "win32" else 60)
@pytest.mark.asyncio
async def test_process_manager_high_concurrency():
    with ProcessManager(ProcessManagerSpec()) as manager:
        futures = []
        for i in range(32):
            futures.append(manager.submit(simple_task_v, (i,)))
        for i, future in enumerate(futures):
            assert await future == i
