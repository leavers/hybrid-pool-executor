from hybrid_pool_executor.base import ModuleSpec
from hybrid_pool_executor.workers.asyncio.manager import AsyncManager, AsyncManagerSpec
from hybrid_pool_executor.workers.asyncio.worker import (
    AsyncTask,
    AsyncWorker,
    AsyncWorkerSpec,
)

MODULE_SPEC = ModuleSpec(
    name="async",
    manager_type=AsyncManager,
    manager_spec_type=AsyncManagerSpec,
    worker_type=AsyncWorker,
    worker_spec_type=AsyncWorkerSpec,
    tags=frozenset({"async"}),
    enabled=True,
)


__all__ = (
    "MODULE_SPEC",
    "AsyncManager",
    "AsyncManagerSpec",
    "AsyncTask",
    "AsyncWorker",
    "AsyncWorkerSpec",
)
