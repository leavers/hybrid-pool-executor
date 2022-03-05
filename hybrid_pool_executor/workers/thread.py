import dataclasses
import itertools
import typing as t
from dataclasses import dataclass, field
from functools import partial
from queue import Empty, SimpleQueue
from threading import Event, ThreadError
from time import monotonic

from hybrid_pool_executor.base import (
    Action,
    BaseManager,
    BaseManagerSpec,
    BaseTask,
    BaseWorker,
    BaseWorkerSpec,
    CancelledError,
    Future,
    ModuleSpec,
)
from hybrid_pool_executor.constants import (
    ACT_CLOSE,
    ACT_DONE,
    ACT_EXCEPTION,
    ACT_NONE,
    ACT_RESET,
    ACT_RESTART,
    ActionFlag,
    Function,
)
from hybrid_pool_executor.utils import (
    AsyncToSync,
    KillableThread,
    coalesce,
    isasync,
    rectify,
)


@dataclass
class ThreadTask(BaseTask):
    future: Future = field(default_factory=Future)


@dataclass
class ThreadWorkerSpec(BaseWorkerSpec):
    daemon: bool = True


class ThreadWorker(BaseWorker):
    def __init__(self, spec: ThreadWorkerSpec):
        self._spec = dataclasses.replace(spec)
        self._name = self._spec.name

        # bare bool vairiable may not be synced when using start()
        self._state: t.Dict[str, bool] = {
            "inited": False,
            "running": False,
            "idle": False,
        }
        self._thread: t.Optional[KillableThread] = None
        self._current_task_name: t.Optional[str] = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> ThreadWorkerSpec:
        return self._spec

    def is_alive(self) -> bool:
        return self._state["running"]

    def _get_response(
        self,
        flag: ActionFlag = ACT_NONE,
        result: t.Optional[t.Any] = None,
        exception: t.Optional[BaseException] = None,
    ):
        return Action(
            flag=flag,
            task_name=self._current_task_name,
            worker_name=self._name,
            result=result,
            exception=exception,
        )

    def start(self):
        if self._state["running"] or self._thread is not None:
            raise RuntimeError(
                f'{self.__class__.__qualname__} "{self._name}" is already started.'
            )
        self._thread = KillableThread(target=self._run, daemon=self._spec.daemon)
        self._thread.start()

        # Block method until self._run actually starts to avoid creating multiple
        # workers when in high concurrency situation.
        state = self._state
        while not state["inited"]:
            pass

    def _run(self):
        state = self._state
        state["running"] = True
        state["idle"] = True
        state["inited"] = True

        spec = self._spec
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

        response: t.Optional[Action] = None

        idle_tick = monotonic()
        while True:
            if monotonic() - idle_tick > idle_timeout:
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
            if not state["running"]:
                break

            try:
                task: ThreadTask = task_bus.get(timeout=wait_interval)
            except Empty:
                continue
            result = None
            try:
                state["idle"] = False
                self._current_task_name = task.name

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

                self._current_task_name = None
                state["idle"] = True

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

        state["idle"] = False
        state["running"] = False
        if response is not None and response.flag != ACT_NONE:
            response_bus.put(response)

    def stop(self):
        self._state["running"] = False
        if self._thread and self._thread.is_alive():
            self._thread.join()
        self._thread = None

    def terminate(self):
        self._state["running"] = False
        try:
            if self._thread and self._thread.is_alive():
                self._thread.terminate()
        except ThreadError:
            pass
        self._thread = None

    def is_idle(self) -> bool:
        state = self._state
        return state["idle"] if state["running"] else False


@dataclass
class ThreadManagerSpec(BaseManagerSpec):
    mode: str = "thread"
    name_pattern: str = "ThreadManager-{manager_seq}"
    # -1: unlimited; 0: same as num_workers
    max_processing_responses_per_iteration: int = -1
    worker_name_pattern: str = "ThreadWorker-{worker} [{manager}]"
    task_name_pattern: str = "ThreadTask-{task} [{manager}]"
    default_worker_spec: ThreadWorkerSpec = field(
        default_factory=partial(ThreadWorkerSpec, name="DefaultWorkerSpec")
    )


class ThreadManager(BaseManager):
    _next_manager_seq = itertools.count().__next__

    def __init__(self, spec: ThreadManagerSpec):
        self._spec = dataclasses.replace(spec)
        self._default_worker_spec = dataclasses.replace(spec.default_worker_spec)
        self._name = self._spec.name_pattern.format(
            manager_seq=self._next_manager_seq()
        )

        self._next_worker_seq = itertools.count().__next__
        self._next_task_seq = itertools.count().__next__

        self._state: t.Dict[str, bool] = {
            "inited": False,
            "running": False,
        }

        self._task_bus = SimpleQueue()
        self._response_bus = SimpleQueue()
        self._current_workers: t.Dict[str, ThreadWorker] = {}
        self._current_tasks: t.Dict[str, t.Any] = {}
        self._thread: t.Optional[KillableThread] = None

    def start(self):
        if self._state["running"] or self._thread is not None:
            raise RuntimeError(f'ThreadManager "{self._name}" is already started.')
        self._thread = KillableThread(target=self._run, daemon=True)
        self._thread.start()

        state = self._state
        while not state["inited"]:
            pass

    def is_alive(self) -> bool:
        return self._state["running"]

    def _run(self):
        state = self._state
        state["running"] = True
        state["inited"] = True

        metronome = Event()
        wait_interval: float = rectify(coalesce(self._spec.wait_interval, 0.1), 0.1)
        num_process_limit: int = rectify(
            coalesce(self._spec.max_processing_responses_per_iteration, -1), -1
        )
        consume_response = self._consume_response
        response_bus = self._response_bus
        while True:
            if not state["running"]:
                break

            num_processed: int = 0
            while not response_bus.empty():
                consume_response()
                num_processed += 1
                if num_processed >= num_process_limit:
                    break
            if num_processed == 0:
                metronome.wait(wait_interval)
        while not response_bus.empty():
            consume_response()
        self._stop_all_workers()

    def _stop_all_workers(self):
        stop_action = Action(ACT_CLOSE)
        for worker in self._current_workers.values():
            worker.spec.request_bus.put(stop_action)
        for worker in self._current_workers.values():
            worker.stop()

    def stop(self, timeout: float = 5.0):
        self._state["running"] = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=rectify(coalesce(timeout, 5.0), 5.0))
        self._thread = None

    def terminate(self):
        self._state["running"] = False
        try:
            if self._thread and self._thread.is_alive():
                self._thread.terminate()
        except ThreadError:
            pass
        self._thread = None

    def get_worker_spec(
        self,
        name: t.Optional[str] = None,
        daemon: t.Optional[bool] = None,
        idle_timeout: t.Optional[float] = None,
        wait_interval: t.Optional[float] = None,
        max_task_count: t.Optional[int] = None,
        max_err_count: t.Optional[int] = None,
        max_cons_err_count: t.Optional[int] = None,
    ) -> ThreadWorkerSpec:
        if name and name in self._current_tasks:
            raise KeyError(f'Worker "{name}" exists.')
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

    def _get_task_name(self, name: t.Optional[str] = None) -> str:
        if name:
            if name in self._current_tasks:
                raise KeyError(f'Task "{name}" exists.')
            return name
        return coalesce(
            name,
            self._spec.task_name_pattern.format(
                manager=self._name,
                task=self._next_task_seq(),
            ),
        )

    def submit(
        self,
        fn: Function,
        args: t.Optional[t.Iterable[t.Any]] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        name: t.Optional[str] = None,
    ) -> Future:
        if not self._state["running"]:
            raise RuntimeError(
                f'Manager "{self._name}" is either stopped or not started yet '
                "and not able to accept tasks."
            )
        name = self._get_task_name(name)
        future = Future()
        task = ThreadTask(
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

    def _consume_response(self):
        response: Action = self._response_bus.get()
        response.task_name = t.cast(str, response.task_name)
        response.worker_name = t.cast(str, response.worker_name)
        if response.match(ACT_DONE, ACT_EXCEPTION):
            self._current_tasks.pop(response.task_name)
        if response.match(ACT_CLOSE):
            self._current_workers.pop(response.worker_name)
        elif response.match(ACT_RESTART):
            worker = self._current_workers[response.worker_name]
            worker.stop()
            worker.start()

    def _adjust_iterator(self) -> range:
        if self._spec.incremental or self._spec.num_workers < 0:
            qsize = self._task_bus.qsize()
            num_idle_workers: int = sum(
                1 if w.is_idle() else 0 for w in self._current_workers.values()
            )
            if self._spec.num_workers < 0:
                iterator = range(qsize - num_idle_workers)
            else:
                num_curr_workers: int = len(self._current_workers)
                iterator = range(
                    num_curr_workers,
                    min(
                        self._spec.num_workers,
                        num_curr_workers + qsize - num_idle_workers,
                    ),
                )
        else:
            iterator = range(len(self._current_workers), self._spec.num_workers)
        return iterator

    def _adjust_workers(self):
        # return if the number of workers already meets requirements
        # works on both incremental and static mode
        if len(self._current_workers) == self._spec.num_workers:
            return
        # if more workers are needed, create them
        for _ in self._adjust_iterator():
            worker = ThreadWorker(self.get_worker_spec())
            self._current_workers[worker._name] = worker
            worker.start()


MODULE_SPEC = ModuleSpec(
    name="thread",
    manager_class=ThreadManager,
    manager_spec_class=ThreadManagerSpec,
    worker_class=ThreadWorker,
    worker_spec_class=ThreadWorkerSpec,
    tags=frozenset({"thread", "async"}),
    enabled=True,
)
