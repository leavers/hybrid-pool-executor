import pytest
import time
import weakref
from random import random
from hybrid_pool_executor.base import (
    ACT_EXCEPTION,
    ACT_RESTART,
)
from hybrid_pool_executor.workers.process import (
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

    ref = weakref.ref(worker)
    del worker_spec, worker, task
    assert ref() is None


def test_process_manager():
    manager_spec = ProcessManagerSpec()
    manager = ProcessManager(manager_spec)
    manager.start()

    future = manager.submit(simple_task)
    assert future.result() == "done"

    manager.stop()


@pytest.mark.asyncio
async def test_process_manager_high_concurrency():
    with ProcessManager(ProcessManagerSpec()) as manager:
        futures = []
        for i in range(32):
            futures.append(manager.submit(simple_task_v, (i,)))
        for i, future in enumerate(futures):
            assert await future == i
