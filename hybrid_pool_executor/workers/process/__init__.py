from hybrid_pool_executor.base import ModuleSpec
from hybrid_pool_executor.workers.process.worker import (
    ProcessManager,
    ProcessManagerSpec,
    ProcessWorker,
    ProcessWorkerSpec,
)

MODULE_SPEC = ModuleSpec(
    name="process",
    manager_class=ProcessManager,
    manager_spec_class=ProcessManagerSpec,
    worker_class=ProcessWorker,
    worker_spec_class=ProcessWorkerSpec,
    tags=frozenset({"process", "thread", "async"}),
    enabled=True,
)
