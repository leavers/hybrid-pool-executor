import pytest
import weakref
from hybrid_pool_executor.base import (
    ACT_EXCEPTION,
    ACT_RESTART,
)
from hybrid_pool_executor.workers.thread import (
    Action,
    ThreadManager,
    ThreadManagerSpec,
    ThreadTask,
    ThreadWorker,
    ThreadWorkerSpec,
)


def test_thread_worker_task():
    def simple_task():
        return "done"

    worker_spec = ThreadWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=1,
    )
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


def test_thread_worker_error():
    def simple_error_task():
        raise RuntimeError("error")

    worker_spec = ThreadWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=1,
    )
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

    assert not worker.is_alive()


def test_thread_worker_cons_error():
    def simple_error_task():
        raise RuntimeError("error")

    worker_spec = ThreadWorkerSpec(
        name="TestThreadWorker",
        idle_timeout=1,
        max_err_count=-1,
        max_cons_err_count=2,
    )

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

    assert not worker.is_alive()


def test_thread_manager():
    def simple_task():
        return "done"

    manager_spec = ThreadManagerSpec()
    manager = ThreadManager(manager_spec)
    manager.start()

    future = manager.submit(simple_task)
    assert future.result() == "done"

    manager.stop()
