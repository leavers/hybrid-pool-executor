import time
import pytest
import weakref
from hybrid_pool_executor.base import (
    ACT_EXCEPTION,
    ACT_RESTART,
)
from hybrid_pool_executor.workers.async_ import (
    Action,
    AsyncManager,
    AsyncManagerSpec,
    AsyncTask,
    AsyncWorker,
    AsyncWorkerSpec,
)


def test_async_worker_task():
    async def simple_task():
        return "done"

    worker_spec = AsyncWorkerSpec(name="TestAsyncWorker", idle_timeout=1)
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


@pytest.mark.asyncio
async def test_async_worker_task_async_future():
    async def simple_task():
        return "done"

    worker_spec = AsyncWorkerSpec(name="TestAsyncWorker", idle_timeout=1)
    task = AsyncTask(name="simple_task", fn=simple_task)
    worker_spec.task_bus.put(task)

    worker = AsyncWorker(worker_spec)
    worker.start()

    assert await task.future == "done"
    assert task.future.result() == "done"
    assert await task.future == "done"

    worker.stop()


def test_thread_worker_error():
    async def simple_error_task():
        raise RuntimeError("error")

    worker_spec = AsyncWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=1,
    )
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
    assert ref() is None


def test_thread_worker_max_error():
    async def simple_task():
        return "done"

    async def simple_error_task():
        raise RuntimeError("error")

    worker_spec = AsyncWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=2,
        max_cons_err_count=-1,
    )

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


def test_thread_worker_cons_error():
    async def simple_error_task():
        raise RuntimeError("error")

    worker_spec = AsyncWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=-1,
        max_cons_err_count=2,
    )

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


def test_thread_manager():
    async def simple_task():
        return "done"

    manager_spec = AsyncManagerSpec()
    manager = AsyncManager(manager_spec)
    manager.start()

    future = manager.submit(simple_task)
    assert future.result() == "done"

    manager.stop()
