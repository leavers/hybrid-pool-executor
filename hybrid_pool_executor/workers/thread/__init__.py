from hybrid_pool_executor.base import ModuleSpec
from hybrid_pool_executor.workers.thread.worker import (
    ThreadManager,
    ThreadManagerSpec,
    ThreadWorker,
    ThreadWorkerSpec,
)

MODULE_SPEC = ModuleSpec(
    name="thread",
    manager_class=ThreadManager,
    manager_spec_class=ThreadManagerSpec,
    worker_class=ThreadWorker,
    worker_spec_class=ThreadWorkerSpec,
    tags=frozenset({"thread", "async"}),
    enabled=True,
)
