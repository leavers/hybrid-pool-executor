import inspect
import itertools
import dataclasses
from dataclasses import dataclass
from queue import Empty, SimpleQueue
from threading import Condition, Thread
from time import monotonic
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple, Union
from hybrid_pool_executor.base import (
    BaseAction,
    BaseAsyncFutureInterface,
    BaseFuture,
    BaseManager,
    BaseManagerSpec,
    BaseTask,
    BaseWorker,
    BaseWorkerSpec,
    CancelledError,
    ModuleSpec,
)
from hybrid_pool_executor.typing import (
    ActionFlag,
    ACT_DONE,
    ACT_EXCEPTION,
    ACT_NONE,
    ACT_CLOSE,
    ACT_CMD_WORKER_STOP_FLAGS,
    ACT_RESET,
    ACT_RESTART,
    Function,
)
from hybrid_pool_executor.utils import coalesce, rectify


class ThreadAsyncFutureInterface(BaseAsyncFutureInterface):
    pass


class ThreadFuture(BaseFuture):
    def __init__(self):
        self._condition = Condition()
        self._result = None
        self._exception = None


@dataclass
class ThreadAction(BaseAction):
    pass


@dataclass
class ThreadTask(BaseTask):
    future: Optional[ThreadFuture] = None


@dataclass
class ThreadWorkerSpec(BaseWorkerSpec):
    daemon: bool = True


class ThreadWorker(BaseWorker):
    def __init__(self, spec: ThreadWorkerSpec):
        self.name = spec.name
        self.spec = spec

        self._running: bool = False
        self._idle: bool = True
        self._braking: bool = True
        self._thread: Optional[Thread] = None
        self._task_id: Optional[str] = None

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

    def start(self):
        if not self._braking:
            raise RuntimeError(
                f"{self.__class__.__qualname__} {self.name} is already started"
            )
        self._thread = Thread(target=self.run, daemon=self.spec.daemon)
        self._thread.start()

        # Block method until self.run actually starts to avoid creating multiple
        # workers when in high concurrency situation.
        while self._braking:
            pass

    def run(self):
        self._braking = False
        self._running = True

        spec = self.spec
        get_response = self._get_response
        task_bus: SimpleQueue = spec.task_bus
        request_bus: SimpleQueue = spec.request_bus
        response_bus: SimpleQueue = spec.response_bus
        max_task_count: int = spec.max_task_count
        max_err_count: int = spec.max_err_count
        max_cons_err_count: int = spec.max_cons_err_count
        idle_timeout: float = spec.idle_timeout
        wait_interval: float = spec.wait_interval

        task_count: int = 0
        err_count: int = 0
        cons_err_count: int = 0

        response: Optional[ThreadAction] = None

        idle_tick = monotonic()
        while True:
            if monotonic() - idle_tick > idle_timeout:
                response = get_response(ACT_CLOSE)
                break
            while not request_bus.empty():
                request: ThreadAction = request_bus.get()
                if request.match(ACT_RESET):
                    task_count = 0
                    err_count = 0
                    cons_err_count = 0
                if request.match(ACT_CMD_WORKER_STOP_FLAGS):
                    response = get_response(request.flag)
                    self._braking = True
                    break
                if self._braking:
                    break

                try:
                    order: ThreadTask = task_bus.get(timeout=wait_interval)
                except Empty:
                    continue
                result = None
                try:
                    self._idle = False
                    self._task_id = order.name

                    # check if order is cancelled
                    if order.cancelled:
                        raise CancelledError(
                            f'Future "{order.name}" has been cancelled'
                        )

                    result = order.func(*order.args, **order.kwargs)
                except Exception as exc:
                    err_count += 1
                    cons_err_count += 1
                    response = get_response(
                        flag=ACT_EXCEPTION,
                        result=result,
                        exception=exc,
                    )
                else:
                    cons_err_count = 0
                    response = get_response(flag=ACT_DONE, result=result)
                finally:
                    task_count += 1

                    self._task_id = None
                    self._idle = True

                    idle_tick = monotonic()
                    if (
                        0 <= max_task_count <= task_count
                        or 0 <= max_err_count <= err_count
                        or 0 <= max_cons_err_count <= cons_err_count
                    ):
                        response.add_flag(ACT_RESTART)
                        break
                    response_bus.put(response)
                    response = None

            if response is not None and response.flag != ACT_NONE:
                response_bus.put(response)
            self._running = False
            self._braking = True

    def stop(self):
        self._braking = True
        if self._thread and self._thread.is_alive():
            self._thread.join()
        self._running = False
        self._thread = None

    def idle(self) -> bool:
        return self._idle


@dataclass
class ThreadManagerSpec(BaseManagerSpec):
    mode: str = "thread"
    name_pattern: str = "ThreadWorker-{manager}-{worker}"


class ThreadManager(BaseManager):
    _next_manager_seq = itertools.count().__next__

    def __init__(self, spec: ThreadManagerSpec):
        self._spec = dataclasses.replace(spec)
        self._name = self._spec.name_pattern.format(manager=self._next_manager_seq())
        self._next_worker_seq = itertools.count().__next__
        self._task_bus = SimpleQueue()
        self._running_tasks: Dict[str, Any] = {}

    def get_worker_spec(
        self,
        name: Optional[str] = None,
        daemon: Optional[bool] = None,
        idle_timeout: Optional[float] = None,
        wait_interval: Optional[float] = None,
        max_task_count: Optional[int] = None,
        max_err_count: Optional[int] = None,
        max_cons_err_count: Optional[int] = None,
    ) -> ThreadWorkerSpec:
        worker_spec = ThreadWorkerSpec(
            name=coalesce(
                name,
                self._spec.worker_name_pattern.format(
                    manager=self._name,
                    worker=self._next_worker_seq(),
                ),
            ),
            task_bus=self._task_bus,
            request_bus=SimpleQueue(),
            response_bus=SimpleQueue(),
            daemon=coalesce(daemon, True),
            idle_timeout=rectify(coalesce(idle_timeout, 60.0)),
            wait_interval=rectify(coalesce(wait_interval, 0.1), 0.1),
            max_task_count=rectify(coalesce(max_task_count, 12), -1),
            max_err_count=rectify(coalesce(max_err_count, 3), -1),
            max_cons_err_count=rectify(coalesce(max_cons_err_count, -1), -1),
        )
        return worker_spec

    def _get_task_name(self, name: str = None) -> str:
        return coalesce(
            name,
            self._spec.worker_name_pattern.format(
                manager=self._name,
                worker=self._next_worker_seq(),
            ),
        )

    def submit(
        self,
        func: Function,
        args: Tuple[Any, ...] = (),
        kwargs: Dict[str, Any] = None,
        name: str = None,
    ) -> ThreadFuture:
        if inspect.iscoroutinefunction(func) or inspect.iscoroutine(func):
            raise NotImplementedError("Coroutine function is not supported yet.")
        if name in self._running_tasks:
            raise KeyError(f'Task "{name}" exists.')
        future = ThreadFuture()
        task = ThreadTask(
            name=self._get_task_name(name),
            func=func,
            args=args,
            kwargs=kwargs or {},
            future=future,
        )
        self._task_bus.put(task)
        return future

    def consume_response(self, response: ThreadAction):
        pass

    def _adjust_workers(self):
        pass

    def shutdown(self):
        pass


IMPORT_SPEC = ModuleSpec(
    name="thread",
    manager_class=ThreadManager,
    manager_spec_class=ThreadManagerSpec,
    worker_class=ThreadWorker,
    worker_spec_class=ThreadWorkerSpec,
)
