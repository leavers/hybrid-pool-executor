import typing as t
from dataclasses import dataclass, field
from functools import partial

from hybrid_pool_executor.workers.asyncio.worker import (
    AsyncTask,
    AsyncWorker,
    AsyncWorkerSpec,
)
from hybrid_pool_executor.workers.thread import (
    ThreadManager,
    ThreadManagerSpec,
    ThreadWorker,
)

NoneType = type(None)


@dataclass
class AsyncManagerSpec(ThreadManagerSpec):
    name_pattern: str = "AsyncManager-{manager_seq}"
    worker_name_pattern: str = "AsyncWorker-{worker_seq} [{manager}]"
    task_name_pattern: str = "AsyncTask-{task_seq} [{manager}]"
    num_workers: int = 1
    task_class: t.Type[AsyncTask] = AsyncTask
    worker_class: t.Type[AsyncWorker] = AsyncWorker
    worker_spec: AsyncWorkerSpec = field(
        default_factory=partial(AsyncWorkerSpec, name="DefaultWorkerSpec")
    )


class AsyncManager(ThreadManager):
    def __init__(self, spec: AsyncManagerSpec):
        super().__init__(spec=t.cast(ThreadManagerSpec, spec))
        self._spec = t.cast(AsyncManagerSpec, self._spec)
        self._worker_spec = t.cast(AsyncWorkerSpec, self._worker_spec)
        self._curr_workers = t.cast(t.Dict[str, ThreadWorker], self._curr_workers)

    def get_worker_spec(
        self,
        name: t.Optional[str] = None,
        daemon: t.Optional[bool] = None,
        idle_timeout: t.Optional[float] = None,
        wait_interval: t.Optional[float] = None,
        max_task_count: t.Optional[int] = None,
        max_err_count: t.Optional[int] = None,
        max_cons_err_count: t.Optional[int] = None,
    ) -> AsyncWorkerSpec:
        return t.cast(
            AsyncWorkerSpec,
            super().get_worker_spec(
                name=name,
                daemon=daemon,
                idle_timeout=idle_timeout,
                wait_interval=wait_interval,
                max_task_count=max_task_count,
                max_err_count=max_err_count,
                max_cons_err_count=max_cons_err_count,
            ),
        )
