from hybrid_pool_executor.base import ModuleSpec
from hybrid_pool_executor.workers.thread.manager import ThreadManager, ThreadManagerSpec
from hybrid_pool_executor.workers.thread.worker import (
    ThreadTask,
    ThreadWorker,
    ThreadWorkerSpec,
)

MODULE_SPEC = ModuleSpec(
    name="thread",
    manager_type=ThreadManager,
    manager_spec_type=ThreadManagerSpec,
    worker_type=ThreadWorker,
    worker_spec_type=ThreadWorkerSpec,
    tags=frozenset({"thread", "async"}),
    enabled=True,
)


__all__ = (
    "MODULE_SPEC",
    "ThreadManager",
    "ThreadManagerSpec",
    "ThreadTask",
    "ThreadWorker",
    "ThreadWorkerSpec",
)
