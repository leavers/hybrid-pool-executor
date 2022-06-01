from hybrid_pool_executor.base import ModuleSpec
from hybrid_pool_executor.workers.asyncio.worker import (
    AsyncManager,
    AsyncManagerSpec,
    AsyncWorker,
    AsyncWorkerSpec,
)

MODULE_SPEC = ModuleSpec(
    name="async",
    manager_class=AsyncManager,
    manager_spec_class=AsyncManagerSpec,
    worker_class=AsyncWorker,
    worker_spec_class=AsyncWorkerSpec,
    tags=frozenset({"async"}),
    enabled=True,
)
