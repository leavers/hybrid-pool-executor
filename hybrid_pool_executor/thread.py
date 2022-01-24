from dataclasses import dataclass
from hybrid_pool_executor.base import WorkerSpec
from queue import SimpleQueue
from threading import Thread
from typing import Hashable, Optional


@dataclass
class ThreadWorkerSpec(WorkerSpec):
    pass


class ThreadWorker:
    def __init__(self, spec: ThreadWorkerSpec):
        self.spec = spec
        self.name: Hashable = self.spec.name
        self.task_queue: SimpleQueue = self.spec.task_queue
        self.request_queue: SimpleQueue = self.spec.request_queue
        self.response_queue: SimpleQueue = self.spec.response_queue
        self.daemon: bool = self.spec.daemon
        self.idle_timeout: float = self.spec.idle_timeout
        self.wait_interval: float = self.spec.wait_interval
        self.max_task_count: int = self.spec.max_task_count
        self.max_err_count: int = self.spec.max_err_count
        self.max_cons_err_count: int = self.spec.max_cons_err_count

        # whether worker is running
        self._running: bool = False
        # whether worker is internal interrupted such as startup or shutdown
        self._interrupted: bool = True
        # whether worker is idle
        self._idle: bool = True

        # id of task the worker is processing on
        self._task_id: Optional[Hashable] = None
        # the thread of worker
        self._thread: Optional[Hashable] = None

    def _start_working_state(self, task_id: Hashable = None):
        self._idle = False
        self._task_id = task_id

    def _stop_working_state(self):
        self._idle = True
        self._task_id = None

