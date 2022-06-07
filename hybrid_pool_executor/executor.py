import atexit
import time
import typing as t
from weakref import WeakSet

from hybrid_pool_executor.base import (
    BaseExecutor,
    BaseManager,
    BaseManagerSpec,
    Future,
    ModuleSpec,
    NotSupportedError,
)
from hybrid_pool_executor.constants import Function
from hybrid_pool_executor.spec import ModuleSpecRepo, spec_factory
from hybrid_pool_executor.utils import isasync, iscoroutine

_all_executors: WeakSet = WeakSet()


def _python_exit():
    for executor in _all_executors:
        if executor.is_alive():
            executor.shutdown()


atexit.register(_python_exit)


class HybridPoolExecutor(BaseExecutor):
    def __init__(
        self,
        thread_workers: int = -1,
        incremental_thread_workers: bool = True,
        thread_worker_name_pattern: t.Optional[str] = None,
        redirect_thread: t.Optional[str] = None,
        process_workers: int = -1,
        incremental_process_workers: bool = True,
        process_worker_name_pattern: t.Optional[str] = None,
        redirect_process: t.Optional[str] = None,
        async_workers: int = -1,
        incremental_async_workers: bool = True,
        async_worker_name_pattern: t.Optional[str] = None,
        redirect_async: t.Optional[str] = None,
        extra_specs: t.Optional[t.Iterable[ModuleSpec]] = None,
        **kwargs,
    ):
        self._spec_repo: ModuleSpecRepo = spec_factory.get_repo()
        if extra_specs is not None:
            for spec in extra_specs:
                self._spec_repo.import_spec(spec, overwrite=True)
        self._managers: t.Dict[str, BaseManager] = {}
        self._manager_kwargs = {
            "thread_workers": thread_workers,
            "incremental_thread_workers": incremental_thread_workers,
            "thread_worker_name_pattern": thread_worker_name_pattern,
            "redirect_thread": redirect_thread,
            "process_workers": process_workers,
            "incremental_process_workers": incremental_process_workers,
            "process_worker_name_pattern": process_worker_name_pattern,
            "redirect_process": redirect_process,
            "async_workers": async_workers,
            "incremental_async_workers": incremental_async_workers,
            "async_worker_name_pattern": async_worker_name_pattern,
            "redirect_async": redirect_async,
            **kwargs,
        }

        self._last_clear_ts: float = time.monotonic()
        self._clear_counter: int = 0

        global _all_executors
        _all_executors.add(self)
        self._is_alive: bool = True

    @property
    def specs(self) -> ModuleSpecRepo:
        return self._spec_repo

    @classmethod
    def _get_manager(
        cls,
        mode: str,
        module_spec: ModuleSpec,
        kwargs: t.Dict[str, t.Any],
    ) -> BaseManager:
        if (redirect := kwargs.get(f"redirect_{mode}")) is not None:
            mode = redirect
        manager_spec: BaseManagerSpec = module_spec.manager_spec_type()
        if (num_workers := kwargs.get(f"{mode}_workers")) is not None:
            manager_spec.num_workers = num_workers
        if (incremental := kwargs.get(f"incremental_{mode}_workers")) is not None:
            manager_spec.incremental = incremental
        if (
            worker_name_pattern := kwargs.get(f"{mode}_worker_name_pattern")
        ) is not None:
            manager_spec.worker_name_pattern = worker_name_pattern
        return module_spec.manager_type(manager_spec)

    def submit(  # type: ignore
        self,
        fn: t.Callable[..., t.Any],
        /,
        *args,
        **kwargs,
    ) -> Future:
        return self.submit_task(
            fn,
            args=args,
            kwargs=kwargs,
            name=kwargs.get("_name"),
            mode=kwargs.get("_mode"),
            tags=kwargs.get("_tags"),
        )

    def apply_async(
        self,
        func: t.Callable[..., t.Any],
        args: t.Iterable[t.Any] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
    ) -> Future:
        return self.submit(func, *args, **(kwargs or {}))

    def apply(
        self,
        func: t.Callable[..., t.Any],
        args: t.Iterable[t.Any] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
    ) -> t.Any:
        return self.submit(func, *args, **(kwargs or {})).result()

    def map_tasks(
        self,
        fn: t.Callable[..., t.Any],
        *iterables: t.Union[t.Iterable[t.Any], t.Mapping[str, t.Any], t.Any],
        timeout: t.Optional[float] = None,
        mode: t.Optional[str] = None,
        tags: t.Optional[t.Iterable[str]] = None,
    ) -> t.Generator[t.Any, None, None]:
        if timeout is not None:
            end_time = timeout + time.monotonic()
        fs = []
        for params in iterables:
            if isinstance(params, t.Mapping):
                fs.append(
                    self.submit_task(
                        fn=fn,
                        kwargs=t.cast(t.Dict[str, t.Any], params),
                        name=params.get("_name"),
                        mode=params.get("_mode", mode),
                        tags=params.get("_tags", tags),
                    )
                )
            else:
                if not isinstance(params, t.Iterable):
                    params = [params]
                fs.append(self.submit_task(fn=fn, args=params, mode=mode, tags=tags))

        def result_iterator() -> t.Any:
            try:
                fs.reverse()
                while fs:
                    if timeout is None:
                        yield fs.pop().result()
                    else:
                        yield fs.pop().result(end_time - time.monotonic())
            finally:
                for future in fs:
                    future.cancel()

        return result_iterator()

    @t.overload
    def submit_task(
        self,
        fn: t.Coroutine[t.Any, t.Any, t.Any],
        *,
        name: t.Optional[str] = None,
        mode: t.Optional[str] = None,
        tags: t.Optional[t.Iterable[str]] = None,
    ) -> Future:
        ...

    @t.overload
    def submit_task(
        self,
        fn: t.Callable[..., t.Any],
        args: t.Optional[t.Iterable[t.Any]] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        name: t.Optional[str] = None,
        mode: t.Optional[str] = None,
        tags: t.Optional[t.Iterable[str]] = None,
    ) -> Future:
        ...

    def submit_task(
        self,
        fn: Function,
        args: t.Optional[t.Iterable[t.Any]] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        name: t.Optional[str] = None,
        mode: t.Optional[str] = None,
        tags: t.Optional[t.Iterable[str]] = None,
        **_,
    ) -> Future:
        if iscoroutine(fn) and (args or kwargs):
            raise ValueError("Coroutine should not have arguments specified.")
        modes = self._guess_mode(fn, mode, tags)
        if not modes:
            raise NotSupportedError(
                (
                    f"No match mode found for task {fn}{name} with requirements: "
                    f'["mode={mode}, tags={tags}"].'
                ).format(
                    fn=fn,
                    name=f' "{name}"' if name else "",
                    mode=mode,
                    tags=tags,
                )
            )
        mode = modes.__iter__().__next__()
        manager = self._managers.get(mode)
        if not manager or not manager.is_alive():
            module_spec: ModuleSpec = self._spec_repo[mode]
            manager = self._get_manager(
                mode=mode,
                module_spec=module_spec,
                kwargs=self._manager_kwargs,
            )
            self._managers[mode] = manager
        self._is_alive = True
        if not manager.is_alive():
            manager.start()
        self._clear_manager_if_needed()
        return (
            manager.submit(
                fn=t.cast(t.Coroutine[t.Any, t.Any, t.Any], fn),
                name=name,
            )
            if iscoroutine(fn)
            else manager.submit(
                fn=t.cast(t.Callable[..., t.Any], fn),
                args=args,
                kwargs=kwargs,
                name=name,
            )
        )

    def _clear_manager_if_needed(self) -> None:
        self._clear_counter += 1
        monotonic = time.monotonic()
        now, prev, self._last_clear_ts = monotonic, self._last_clear_ts, monotonic
        if self._clear_counter <= 256 and now - prev < 300:
            return
        self._clear_counter = 0
        cleared = []
        for name, manager in self._managers.items():
            if not manager.is_alive():
                cleared.append(name)
        for name in cleared:
            self._managers.pop(name)

    def _guess_mode(
        self,
        fn: Function,
        mode: t.Optional[str] = None,
        tags: t.Optional[t.Iterable[str]] = None,
    ) -> t.FrozenSet[str]:
        if tags:
            mode_filter = self._spec_repo.filter_by_tags(*tags)
        else:
            mode_filter = None
        if mode:
            if mode_filter is not None:
                res = {mode} if mode in mode_filter else frozenset()
            else:
                res = {mode}
        else:
            if mode_filter is not None:
                res = mode_filter
            else:
                res = {"async"} if isasync(fn) else {"thread"}
        if res == {"async"} and not isasync(fn):
            return frozenset()
        return frozenset(res)

    def is_alive(self) -> bool:
        return self._is_alive

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False):
        for manager in self._managers.values():
            if wait:
                manager.stop()
            else:
                manager.terminate()
        self._is_alive = False
