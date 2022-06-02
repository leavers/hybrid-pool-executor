from hybrid_pool_executor.base import ModuleSpec
from hybrid_pool_executor.workers.process.manager import (
    ProcessManager,
    ProcessManagerSpec,
)
from hybrid_pool_executor.workers.process.worker import (
    ProcessTask,
    ProcessWorker,
    ProcessWorkerSpec,
)

MODULE_SPEC = ModuleSpec(
    name="process",
    manager_type=ProcessManager,
    manager_spec_type=ProcessManagerSpec,
    worker_type=ProcessWorker,
    worker_spec_type=ProcessWorkerSpec,
    tags=frozenset({"process", "thread", "async"}),
    enabled=True,
)


__all__ = (
    "MODULE_SPEC",
    "ProcessManager",
    "ProcessManagerSpec",
    "ProcessTask",
    "ProcessWorker",
    "ProcessWorkerSpec",
)
