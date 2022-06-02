import dataclasses
import typing as t
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from threading import ThreadError
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
    ThreadBus,
    ThreadBusType,
)
from hybrid_pool_executor.utils import (
    AsyncToSync,
    KillableThread,
    WeakClassMethod,
    isasync,
)


@dataclass
class ThreadTask(BaseTask):
    future: Future = field(default_factory=Future)


@dataclass
class ThreadWorkerSpec(BaseWorkerSpec):
    """The specification of thread worker.

    :param task_bus: The queue for sending task item.
    :type task_bus: SimpleQueue

    :param request_bus: The queue for receiving requests from manager.
    :type request_bus: SimpleQueue

    :param response_bus: The queue for sending responses to manager.
    :type response_bus: SimpleQueue

    :param daemon: True if worker should be a daemon, defaults to True.
    :type daemon: bool, optional
    """

    task_bus_type: ThreadBusType = SimpleQueue
    request_bus_type: ThreadBusType = SimpleQueue
    response_bus_type: ThreadBusType = SimpleQueue
    task_bus: t.Optional[ThreadBus] = None
    request_bus: t.Optional[ThreadBus] = None
    response_bus: t.Optional[ThreadBus] = None
    daemon: bool = True


class ThreadWorker(BaseWorker):
    def __init__(self, spec: ThreadWorkerSpec) -> None:
        super().__init__()
        self._spec = dataclasses.replace(spec)
        self._name = self._spec.name
        self._thread: t.Optional[KillableThread] = None
        self._curr_task_name: t.Optional[str] = None

        for checkpoint in ("task_bus", "request_bus", "response_bus"):
            if not getattr(self._spec, checkpoint):
                raise ValueError(f'Param "{checkpoint}" in spec is empty.')

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> ThreadWorkerSpec:
        return self._spec

    def _get_response(
        self,
        flag: ActionFlag = ACT_NONE,
        result: t.Optional[t.Any] = None,
        exception: t.Optional[BaseException] = None,
    ) -> Action:
        return Action(
            flag=flag,
            task_name=self._curr_task_name,
            worker_name=self._name,
            result=result,
            exception=exception,
        )

    def start(self):
        if self._state.running or self._thread is not None:
            raise RuntimeError(
                f'{self.__class__.__qualname__} "{self._name}" is already started.'
            )
        self._thread = KillableThread(
            target=WeakClassMethod(self._run),
            daemon=self._spec.daemon,
        )
        self._thread.start()

        # Block method until self._run actually starts to avoid creating multiple
        # workers when in high concurrency situation.
        state = self._state
        while not state.inited:
            pass

    def _run(self):
        state = self._state
        state.running = True
        state.idle = True
        state.inited = True

        spec = self._spec
        get_response = self._get_response
        task_bus: ThreadBus = spec.task_bus
        request_bus: ThreadBus = spec.request_bus
        response_bus: ThreadBus = spec.response_bus
        max_task_count: int = spec.max_task_count
        max_err_count: int = spec.max_err_count
        max_cons_err_count: int = spec.max_cons_err_count
        idle_timeout: float = spec.idle_timeout
        wait_interval: float = spec.wait_interval

        task_count: int = 0
        err_count: int = 0
        cons_err_count: int = 0

        response: t.Optional[Action] = None

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
                    break
            if not state.running:
                break

            try:
                task: ThreadTask = task_bus.get(timeout=wait_interval)
            except Empty:
                continue
            result = None
            try:
                state.idle = False
                self._curr_task_name = task.name

                # check if order is cancelled
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
                task.future.set_exception(exc)
                response = get_response(flag=ACT_EXCEPTION)
            else:
                cons_err_count = 0
                task.future.set_result(result)
                response = get_response(flag=ACT_DONE)
            finally:
                del task
                task_count += 1

                self._curr_task_name = None
                state.idle = True

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

        state.idle = False
        state.running = False
        if response is not None and response.flag != ACT_NONE:
            response_bus.put(response)
        self._thread = None

    def stop(self):
        self._state.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join()
        self._thread = None

    def terminate(self):
        self._state.running = False
        try:
            if self._thread and self._thread.is_alive():
                self._thread.terminate()
        except ThreadError:
            pass
        self._thread = None
