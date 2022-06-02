import dataclasses
import multiprocessing as mp
import typing as t
from dataclasses import dataclass, field
from functools import partial
from multiprocessing import Process, Value
from multiprocessing.sharedctypes import Synchronized
from queue import Empty
from threading import Event
from time import monotonic

from hybrid_pool_executor.base import (
    Action,
    BaseTask,
    BaseWorker,
    BaseWorkerSpec,
    CancelledError,
    Future,
)
from hybrid_pool_executor.constants import (
    ACT_CLOSE,
    ACT_DONE,
    ACT_EXCEPTION,
    ACT_NONE,
    ACT_RESET,
    ACT_RESTART,
    ActionFlag,
    ProcessBus,
    ProcessBusType,
)
from hybrid_pool_executor.utils import AsyncToSync, isasync
from hybrid_pool_executor.workers.process.queue import Queue


@dataclass
class ProcessTask(BaseTask):
    future: t.Optional[Future] = None


@dataclass
class ProcessWorkerSpec(BaseWorkerSpec):
    task_bus_type: ProcessBusType = Queue
    request_bus_type: ProcessBusType = Queue
    response_bus_type: ProcessBusType = Queue
    task_bus: t.Optional[ProcessBus] = None
    request_bus: t.Optional[ProcessBus] = None
    response_bus: t.Optional[ProcessBus] = None
    task_bus_qsize: Synchronized = field(
        default_factory=t.cast(
            t.Callable[[], Synchronized],
            partial(Value, "L", 0),
        )
    )
    daemon: bool = False


class ProcessWorker(BaseWorker):
    def __init__(self, spec: ProcessWorkerSpec):
        self._spec = dataclasses.replace(spec)
        # TODO: change process name in system monitor
        self._name = self._spec.name

        self._inited = mp.Event()
        self._running = mp.Event()
        self._idle = mp.Event()

        self._process: t.Optional[Process] = None
        self._curr_task_name: t.Optional[str] = None

        for checkpoint in ("task_bus", "request_bus", "response_bus"):
            if not getattr(self._spec, checkpoint):
                raise ValueError(f'Param "{checkpoint}" in spec is empty.')

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> ProcessWorkerSpec:
        return self._spec

    def is_alive(self) -> bool:
        return self._running.is_set()

    def is_idle(self) -> bool:
        return self._idle.is_set()

    def start(self):
        if self._running.is_set() or self._process is not None:
            raise RuntimeError(
                f'{self.__class__.__qualname__} "{self._name}" is already started.'
            )
        self._process = Process(
            target=self._run,
            name=self._name,
            kwargs={
                "spec": self._spec,
                "inited": self._inited,
                "idle": self._idle,
                "running": self._running,
            },
            daemon=self._spec.daemon,
        )
        self._process.start()

        while not self._running.is_set():
            pass

    @staticmethod
    def _run(
        spec: ProcessWorkerSpec,
        inited: Event,
        idle: Event,
        running: Event,
    ):
        inited.set()
        idle.set()
        running.set()

        worker_name = spec.name
        curr_task_name: t.Optional[str] = None

        def get_response(
            flag: ActionFlag = ACT_NONE,
            result: t.Optional[t.Any] = None,
            exception: t.Optional[BaseException] = None,
        ) -> Action:
            return Action(
                flag=flag,
                task_name=curr_task_name,
                worker_name=worker_name,
                result=result,
                exception=exception,
            )

        task_bus = t.cast(ProcessBus, spec.task_bus)
        task_bus_qsize = spec.task_bus_qsize
        request_bus = t.cast(ProcessBus, spec.request_bus)
        response_bus = t.cast(ProcessBus, spec.response_bus)
        max_task_count = spec.max_task_count
        max_err_count = spec.max_err_count
        max_cons_err_count = spec.max_cons_err_count
        idle_timeout = spec.idle_timeout
        wait_interval = spec.wait_interval

        task_count = 0
        err_count = 0
        cons_err_count = 0

        response: t.Optional[Action] = None
        should_exit: bool = False
        idle_tick = monotonic()
        while True:
            if idle_timeout >= 0 and monotonic() - idle_tick > idle_timeout:
                response = get_response(ACT_CLOSE)
                break
            while not request_bus.empty():
                request: Action = request_bus.get()
                if request.match(ACT_RESET):
                    task_count = 0
                    err_count = 0
                    cons_err_count = 0
                if request.match(ACT_CLOSE, ACT_RESTART):
                    response = get_response(request.flag)
                    should_exit = True
                    break
            if should_exit or not running.is_set():
                break
            try:
                task: ProcessTask = task_bus.get(timeout=wait_interval)
                task_bus_qsize.value -= 1
            except Empty:
                continue
            result = None
            try:
                idle.clear()
                curr_task_name = task.name

                # check if future is cancelled
                if task.cancelled:
                    raise CancelledError(f'Future "{task.name}" has been cancelled')
                if isasync(task.fn):
                    task.fn = t.cast(t.Coroutine[t.Any, t.Any, t.Any], task.fn)
                    sync_coro = AsyncToSync(task.fn, *task.args, **task.kwargs)
                    result = sync_coro()
                else:
                    task.fn = t.cast(t.Callable[..., t.Any], task.fn)
                    result = task.fn(*task.args, **task.kwargs)
            except Exception as exc:
                err_count += 1
                cons_err_count += 1
                response = get_response(flag=ACT_EXCEPTION, exception=exc)
            else:
                cons_err_count = 0
                response = get_response(flag=ACT_DONE, result=result)
            finally:
                del task
                task_count += 1

                curr_task_name = None
                idle.set()

                idle_tick = monotonic()
                if (
                    0 <= max_task_count <= task_count
                    or 0 <= max_err_count <= err_count
                    or 0 <= max_cons_err_count <= cons_err_count
                ):
                    response = t.cast(Action, response)
                    response.add_flag(ACT_RESTART)
                    break
                response_bus.put(response)
                response = None

        idle.clear()
        running.clear()
        if response is not None and response.flag != ACT_NONE:
            response_bus.put(response)

    def stop(self):
        self._running.clear()
        if self._process and self._process.is_alive():
            self._process.join()
        self._process = None

    def terminate(self):
        self._running.clear()
        if self._process and self._process.is_alive():
            self._process.terminate()
        self._process = None
