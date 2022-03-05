import dataclasses
import itertools
import multiprocessing as mp
import typing as t
from dataclasses import dataclass, field
from functools import partial
from multiprocessing import Process, Queue
from queue import Empty
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
    Function,
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
)
from hybrid_pool_executor.utils import (
    AsyncToSync,
    KillableThread,
    coalesce,
    isasync,
    rectify,
)


@dataclass
class ProcessTask(BaseTask):
    future: t.Optional[Future] = None


@dataclass
class ProcessWorkerSpec(BaseWorkerSpec):
    task_bus: Queue = field(default_factory=Queue)
    request_bus: Queue = field(default_factory=Queue)
    response_bus: Queue = field(default_factory=Queue)
    max_task_count: int = 1
    daemon: bool = False


class ProcessWorker(BaseWorker):
    def __init__(self, spec: ProcessWorkerSpec):
        self._spec = dataclasses.replace(spec)
        self._name = self._spec.name

        self._inited = mp.Event()
        self._running = mp.Event()
        self._idle = mp.Event()

        self._process: t.Optional[Process] = None
        self._current_task_name: t.Optional[str] = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> ProcessWorkerSpec:
        return self._spec

    def is_alive(self) -> bool:
        return self._running.is_set()

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
        current_task_name: t.Optional[str] = None

        def get_response(
            flag: ActionFlag = ACT_NONE,
            result: t.Optional[t.Any] = None,
            exception: t.Optional[BaseException] = None,
        ) -> Action:
            return Action(
                flag=flag,
                task_name=current_task_name,
                worker_name=worker_name,
                result=result,
                exception=exception,
            )

        task_bus: Queue = spec.task_bus
        request_bus: Queue = spec.request_bus
        response_bus: Queue = spec.response_bus
        max_task_count: int = spec.max_task_count
        max_err_count: int = spec.max_err_count
        max_cons_err_count: int = spec.max_cons_err_count
        idle_timeout: float = spec.idle_timeout
        wait_interval: float = spec.wait_interval

        task_count: int = 0
        err_count: int = 0
        cons_err_count: int = 0

        response: t.Optional[Action] = None
        should_exit: bool = False
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
                    should_exit = True
                    break
            if should_exit or not running.is_set():
                break
            try:
                task: ProcessTask = task_bus.get(timeout=wait_interval)
            except Empty:
                continue
            result = None
            try:
                idle.clear()
                current_task_name = task.name

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

                current_task_name = None
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

    def is_idle(self) -> bool:
        return self._idle.is_set()


@dataclass
class ProcessManagerSpec(BaseManagerSpec):
    mode: str = "process"
    name_pattern: str = "ProcessManager-{manager_seq}"
    # -1: unlimited; 0: same as num_workers
    max_processing_responses_per_iteration: int = -1
    worker_name_pattern: str = "ProcessWorker-{worker} [{manager}]"
    task_name_pattern: str = "ProcessTask-{task} [{manager}]"
    default_worker_spec: ProcessWorkerSpec = field(
        default_factory=partial(ProcessWorkerSpec, name="DefaultWorkerSpec")
    )


class ProcessManager(BaseManager):
    _next_manager_seq = itertools.count().__next__

    def __init__(self, spec: ProcessManagerSpec):
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

        self._task_bus = Queue()
        self._response_bus = Queue()
        self._current_workers: t.Dict[str, ProcessWorker] = {}
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
        # TODO: this part blocks function to make sure all processes' result be
        #       captured, however this may also block the stop/shutdown procedure,
        #       especially when using "with" statement (blocked on __exit__() until all
        #       result has been sent back). Maybe we need a better implementation.
        while self._current_tasks:
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
    ) -> ProcessWorkerSpec:
        if name and name in self._current_tasks:
            raise KeyError(f'Worker "{name}" exists.')
        worker_spec = ProcessWorkerSpec(
            name=coalesce(
                name,
                self._spec.worker_name_pattern.format(
                    manager=self._name,
                    worker=self._next_worker_seq(),
                ),
            ),
            task_bus=self._task_bus,
            request_bus=Queue(),
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
        task = ProcessTask(
            name=name,
            fn=fn,
            args=args or (),
            kwargs=kwargs or {},
            future=future,
        )
        self._current_tasks[name] = task
        # exclude future when sending to other process to reduce size
        self._task_bus.put(dataclasses.replace(task, future=None))
        self._adjust_workers()
        return future

    def _consume_response(self):
        response: Action = self._response_bus.get()
        response.task_name = t.cast(str, response.task_name)
        response.worker_name = t.cast(str, response.worker_name)
        if response.match(ACT_DONE, ACT_EXCEPTION):
            task: ProcessTask = self._current_tasks.pop(response.task_name)
            task.future = t.cast(Future, task.future)
            if response.match(ACT_DONE):
                task.future.set_result(response.result)
            else:
                task.future.set_exception(response.exception)
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
            worker = ProcessWorker(self.get_worker_spec())
            self._current_workers[worker._name] = worker
            worker.start()


MODULE_SPEC = ModuleSpec(
    name="process",
    manager_class=ProcessManager,
    manager_spec_class=ProcessManagerSpec,
    worker_class=ProcessWorker,
    worker_spec_class=ProcessWorkerSpec,
    tags=frozenset({"process", "thread", "async"}),
    enabled=True,
)
