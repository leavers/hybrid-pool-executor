import time
from contextlib import contextmanager
from dataclasses import dataclass
from queue import SimpleQueue
from threading import Thread
from typing import Any, Hashable, Optional
from hybrid_pool_executor.typing import (
    ActionFlag,
    ACT_NONE,
    WorkerMode,
    WORKER_MODE_THREAD,
)
from hybrid_pool_executor.base import (
    BaseAction,
    BaseAsyncFutureInterface,
    BaseFuture,
    BaseCall,
    BaseWorkerSpec,
    BaseWorker,
)


@dataclass
class ThreadWorkerSpec(BaseWorkerSpec):
    pass


@dataclass
class ThreadAction(BaseAction):
    worker_mode: WorkerMode = WORKER_MODE_THREAD


@dataclass
class ThreadCall(BaseCall):
    worker_mode: WorkerMode = WORKER_MODE_THREAD


class ThreadAsyncFutureInterface(BaseAsyncFutureInterface):
    pass


class ThreadFuture(BaseFuture):
    pass


class ThreadWorker(BaseWorker):
    def __init__(self, spec: ThreadWorkerSpec):
        self.name = spec.name
        self.spec = spec

        self._running: bool = False
        self._idle: bool = True
        self._braking: bool = True
        self._thread: Optional[Thread] = None
        self._task_id: Optional[Hashable] = None

    def _get_response(
        self,
        flag: ActionFlag = ACT_NONE,
        result: Optional[Any] = None,
        exception: Optional[BaseException] = None,
    ):
        return ThreadAction(
            flag=flag,
            task_id=self._task_id,
            worker_id=self.name,
            result=result,
            exception=exception,
        )

    def run(self):
        self._braking = False
        ctx = self.ctx
        get_response = self._get_response
        task_bus: SimpleQueue = ctx.task_bus
        request_bus: SimpleQueue = ctx.request_bus
        response_bus: SimpleQueue = ctx.response_bus
        idle_timeout: float = ctx.idle_timeout
        wait_interval: float = ctx.wait_interval

        tasks: int = 0
        errors: int = 0
        cons_errors: int = 0

        response: Optional[ThreadAction] = None
        self._running = True
        idle_tick = time.monotonic()
