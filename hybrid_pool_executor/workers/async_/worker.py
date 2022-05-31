import asyncio
import typing as t
from dataclasses import dataclass, field
from functools import partial
from queue import Empty, SimpleQueue
from time import monotonic

from hybrid_pool_executor.base import Action, CancelledError, Function, Future
from hybrid_pool_executor.constants import (
    ACT_CLOSE,
    ACT_DONE,
    ACT_EXCEPTION,
    ACT_NONE,
    ACT_RESET,
    ACT_RESTART,
)
from hybrid_pool_executor.utils import coalesce, isasync, rectify
from hybrid_pool_executor.workers.thread.worker import (
    ThreadManager,
    ThreadManagerSpec,
    ThreadTask,
    ThreadWorker,
    ThreadWorkerSpec,
)

NoneType = type(None)


@dataclass
class AsyncTask(ThreadTask):
    future: Future = field(default_factory=Future)


@dataclass
class AsyncWorkerSpec(ThreadWorkerSpec):
    max_task_count: int = -1
    max_err_count: int = 10


class AsyncWorker(ThreadWorker):
    def __init__(self, spec: AsyncWorkerSpec):
        super().__init__(spec=t.cast(ThreadWorkerSpec, spec))
        self._spec = t.cast(AsyncWorkerSpec, self._spec)
        self._loop: t.Optional[asyncio.AbstractEventLoop] = None
        self._async_tasks: t.Dict[str, asyncio.Task] = {}
        self._current_tasks: t.Dict[str, AsyncTask] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> AsyncWorkerSpec:
        return self._spec

    async def _async_run(self):
        state = self._state
        state.running = True
        state.idle = True
        state.inited = True

        spec = self._spec
        worker_name: str = self._name
        task_bus: SimpleQueue = spec.task_bus
        request_bus: SimpleQueue = spec.request_bus
        response_bus: SimpleQueue = spec.response_bus
        max_task_count: int = spec.max_task_count
        max_err_count: int = spec.max_err_count
        max_cons_err_count: int = spec.max_cons_err_count
        idle_timeout: float = spec.idle_timeout
        wait_interval: float = spec.wait_interval

        loop = t.cast(asyncio.BaseEventLoop, self._loop)
        async_tasks = self._async_tasks
        async_response_bus = asyncio.Queue()

        async def coroutine(
            task: AsyncTask,
            worker_name: str,
            bus: asyncio.Queue,
        ):
            result = resp = None
            try:
                task.fn = t.cast(t.Callable[..., t.Any], task.fn)
                result = await task.fn(*task.args, **task.kwargs)
            except Exception as exc:
                resp = Action(
                    flag=ACT_EXCEPTION,
                    task_name=task.name,
                    worker_name=worker_name,
                )
                task.future.set_exception(exc)
            else:
                resp = Action(
                    flag=ACT_DONE,
                    task_name=task.name,
                    worker_name=worker_name,
                )
                task.future.set_result(result)
            finally:
                await bus.put(resp)

        task_count: int = 0
        err_count: int = 0
        cons_err_count: int = 0
        current_coroutines: int = 0
        is_prev_coro_err: bool = False

        response = None
        idle_tick = monotonic()
        while True:
            if current_coroutines > 0:
                state.idle = False
                idle_tick = monotonic()
            else:
                state.idle = True
                if monotonic() - idle_tick > idle_timeout:
                    response = Action(flag=ACT_CLOSE, worker_name=worker_name)
                    break
            while not request_bus.empty():
                request: Action = request_bus.get()
                if request.match(ACT_RESET):
                    task_count = 0
                    err_count = 0
                    cons_err_count = 0
                if request.match(ACT_CLOSE, ACT_RESTART):
                    response = Action(flag=request.flag, worker_name=worker_name)
                    break
            if not state.running:
                break
            try:
                while not 0 <= max_task_count <= task_count:
                    task: AsyncTask = task_bus.get(timeout=wait_interval)
                    # check if future is cancelled
                    if task.cancelled:
                        task_bus.put(
                            Action(
                                flag=ACT_EXCEPTION,
                                task_name=task.name,
                                worker_name=worker_name,
                                exception=CancelledError(
                                    f'Future "{task.name}" has been cancelled'
                                ),
                            )
                        )
                        del task
                        continue
                    else:
                        state.idle = False
                        async_task: asyncio.Task = loop.create_task(
                            coroutine(
                                task=task,
                                worker_name=worker_name,
                                bus=async_response_bus,
                            )
                        )
                        async_tasks[task.name] = async_task
                        del task
                    task_count += 1
                    current_coroutines += 1
            except Empty:
                pass
            await asyncio.sleep(0)  # ugly but works
            while not async_response_bus.empty():
                response = await async_response_bus.get()
                await async_tasks.pop(response.task_name)
                current_coroutines -= 1
                if response.match(ACT_EXCEPTION):
                    err_count += 1
                    if is_prev_coro_err:
                        cons_err_count += 1
                    else:
                        cons_err_count = 1
                    is_prev_coro_err = True
                else:
                    cons_err_count = 0
                    is_prev_coro_err = False
                if (
                    0 <= max_task_count <= task_count
                    or 0 <= max_err_count <= err_count
                    or 0 <= max_cons_err_count <= cons_err_count
                ):
                    response.add_flag(ACT_RESTART)
                    response_bus.put(response)
                    state.running = False
                    break
                response_bus.put(response)
                response = None
            if not state.running:
                break
        state.running = False
        for async_task in async_tasks.values():
            if not async_task.done():
                await async_task
        if response is not None and response.flag != ACT_NONE:
            if not response.match(ACT_CLOSE, ACT_RESTART):
                response_bus.put(response)
            else:
                await async_response_bus.put(response)
        while not async_response_bus.empty():
            response = await async_response_bus.get()
            response_bus.put(response)
        self._thread = None

    def _run(self):
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._async_run())


@dataclass
class AsyncManagerSpec(ThreadManagerSpec):
    mode: str = "async"
    name_pattern: str = "AsyncManager-{manager_seq}"
    worker_name_pattern: str = "AsyncWorker-{worker} [{manager}]"
    task_name_pattern: str = "AsyncTask-{task} [{manager}]"
    num_workers: int = 1
    worker_class: t.Type[AsyncWorker] = AsyncWorker
    default_worker_spec: AsyncWorkerSpec = field(
        default_factory=partial(AsyncWorkerSpec, name="DefaultWorkerSpec")
    )


class AsyncManager(ThreadManager):
    def __init__(self, spec: AsyncManagerSpec):
        super().__init__(spec=t.cast(ThreadManagerSpec, spec))
        self._spec = t.cast(AsyncManagerSpec, self._spec)

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
        if name and name in self._current_tasks:
            raise KeyError(f'Worker "{name}" exists.')
        worker_spec = AsyncWorkerSpec(
            name=coalesce(
                name,
                self._spec.worker_name_pattern.format(
                    manager=self._name,
                    worker=self._next_worker_seq(),
                ),
            ),
            task_bus=self._task_bus,
            request_bus=SimpleQueue(),
            response_bus=self._response_bus,
            daemon=coalesce(daemon, self._default_worker_spec.daemon),
            idle_timeout=rectify(
                coalesce(idle_timeout, self._default_worker_spec.idle_timeout),
                self._default_worker_spec.idle_timeout,
            ),
            wait_interval=rectify(
                coalesce(wait_interval, self._default_worker_spec.wait_interval),
                self._default_worker_spec.wait_interval,
            ),
            max_task_count=rectify(
                coalesce(max_task_count, self._default_worker_spec.max_task_count),
                self._default_worker_spec.max_task_count,
            ),
            max_err_count=rectify(
                coalesce(max_err_count, self._default_worker_spec.max_err_count),
                self._default_worker_spec.max_err_count,
            ),
            max_cons_err_count=rectify(
                coalesce(
                    max_cons_err_count, self._default_worker_spec.max_cons_err_count
                ),
                self._default_worker_spec.max_cons_err_count,
            ),
        )
        return worker_spec

    def submit(
        self,
        fn: Function,
        args: t.Optional[t.Iterable[t.Any]] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        name: t.Optional[str] = None,
    ) -> Future:
        if not self._state.running:
            raise RuntimeError(
                f'{self.__class__.__name__} "{self._name}" is either stopped or not '
                "started yet and not able to accept tasks."
            )
        if not isasync(fn):
            raise TypeError(
                f'Param "fn" ({fn}) is neither a coroutine nor a coroutine function.'
            )
        name = self._get_task_name(name)
        future = Future()
        task = AsyncTask(
            name=name,
            fn=fn,
            args=args or (),
            kwargs=kwargs or {},
            future=future,
        )
        self._current_tasks[name] = task
        self._task_bus.put(task)
        self._adjust_workers()
        return future
