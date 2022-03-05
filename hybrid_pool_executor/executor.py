import atexit
import importlib
import os
import time
import typing as t
import weakref

from hybrid_pool_executor.base import (
    BaseExecutor,
    BaseManager,
    BaseManagerSpec,
    ExistsError,
    Future,
    ModuleSpec,
    NotSupportedError,
)
from hybrid_pool_executor.constants import Function
from hybrid_pool_executor.utils import SingletonMeta, isasync

_all_executors = weakref.WeakSet()


@atexit.register
def _python_exit():
    for executor in _all_executors:
        if executor.is_alive():
            executor.shutdown()


class ModuleSpecFactory(metaclass=SingletonMeta):
    def __init__(self):
        self._specs: t.Dict[str, ModuleSpec] = {}
        self._tag_index: t.Dict[str, t.Set[str]] = {}
        # load default module specs
        self._import_default()

    def import_spec(self, spec: ModuleSpec):
        name = spec.name
        if name in self._specs:
            raise ExistsError(f'Found duplicated spec "{name}".')
        self._specs[name] = spec
        for tag in spec.tags:
            if tag not in self._tag_index:
                self._tag_index[tag] = set()
            index = self._tag_index[tag]
            index.add(name)

    def import_module(self, module: str):
        module_spec: ModuleSpec = t.cast(
            ModuleSpec, importlib.import_module(module).MODULE_SPEC
        )
        self.import_spec(module_spec)

    def _import_default(self):
        package_name = "workers"
        currdir = os.path.dirname(os.path.abspath(__file__))
        package = os.path.basename(currdir)
        package_path = os.path.join(currdir, package_name)
        for item in os.listdir(package_path):
            if item.startswith("_") or not item.endswith(".py"):
                continue
            if not os.path.isfile(os.path.join(package_path, item)):
                continue
            self.import_module(".".join([package, package_name, item[:-3]]))

    def filter_by_tags(self, *tags: str) -> t.Optional[t.FrozenSet[str]]:
        if not tags:
            return frozenset()
        filter = set()
        for tag in tags:
            index = self._tag_index.get(tag)
            if not index:
                return frozenset()
            if not filter:
                filter |= index
            else:
                filter &= index
        return frozenset(filter)

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def __getitem__(self, name: str):
        return self._specs[name]

    def get(self, name: str):
        return self._specs.get(name)

    def pop(self, name: str, default=None):
        return self._specs.pop(name, default)


module_spec_factory = ModuleSpecFactory()


class HybridPoolExecutor(BaseExecutor):
    def __init__(
        self,
        thread_workers: int = -1,
        incremental_thread_workers: bool = True,
        thread_worker_name_pattern: t.Optional[str] = None,
        redirect_thread: t.Optional[str] = None,
        **kwargs,
    ):
        self._module_specs: ModuleSpecFactory = module_spec_factory
        self._managers: t.Dict[str, BaseManager] = {}
        self._manager_kwargs = {
            "thread_workers": thread_workers,
            "incremental_thread_workers": incremental_thread_workers,
            "thread_worker_name_pattern": thread_worker_name_pattern,
            "redirect_thread": redirect_thread,
            **kwargs,
        }

        global _all_executors
        _all_executors.add(self)
        self._is_alive: bool = True

    @classmethod
    def _get_manager(
        cls,
        mode: str,
        module_spec: ModuleSpec,
        kwargs: t.Dict[str, t.Any],
    ) -> BaseManager:
        if (redirect := kwargs.get(f"redirect_{mode}")) is not None:
            mode = redirect
        manager_spec: BaseManagerSpec = module_spec.manager_spec_class(mode=mode)
        if (num_workers := kwargs.get(f"{mode}_workers")) is not None:
            manager_spec.num_workers = num_workers
        if (incremental := kwargs.get(f"incremental_{mode}_workers")) is not None:
            manager_spec.incremental = incremental
        if (
            worker_name_pattern := kwargs.get(f"{mode}_worker_name_pattern")
        ) is not None:
            manager_spec.worker_name_pattern = worker_name_pattern
        return module_spec.manager_class(manager_spec)

    def submit(self, fn: t.Callable[..., t.Any], /, *args, **kwargs) -> Future:
        mode = kwargs.pop("_mode", None)
        return self.submit_task(fn, args=args, kwargs=kwargs, mode=mode)

    def map_tasks(
        self,
        fn: t.Callable[..., t.Any],
        *iterables: t.Union[t.Iterable[t.Any], t.Mapping[str, t.Any], t.Any],
        timeout: t.Optional[float] = None,
    ):
        if timeout is not None:
            end_time = timeout + time.monotonic()
        fs = []
        for params in iterables:
            if isinstance(params, t.Mapping):
                fs.append(self.submit(fn, **params))
            else:
                if not isinstance(params, t.Iterable):
                    params = [params]
                fs.append(self.submit(fn, *params))

        def result_iterator():
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

    def submit_task(
        self,
        fn: Function,
        args: t.Optional[t.Iterable[t.Any]] = (),
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        name: t.Optional[str] = None,
        mode: t.Optional[str] = None,
        tags: t.Optional[t.Iterable[str]] = None,
        **ignored,
    ) -> Future:
        modes = self._guess_mode(fn, mode, tags)
        if not modes:
            raise NotSupportedError(
                (
                    "No match mode found for task {fn}{name} with requirements: "
                    '["mode={mode}, tags={tags}"].'
                ).format(
                    fn=fn,
                    name=f' "{name}"' if name else "",
                    mode=mode,
                    tags=tags,
                )
            )
        mode = modes.__iter__().__next__()
        manager = self._managers.get(mode)
        if not manager:
            module_spec: ModuleSpec = self._module_specs[mode]
            manager = self._get_manager(
                mode=mode,
                module_spec=module_spec,
                kwargs=self._manager_kwargs,
            )
            self._managers[mode] = manager
        self._is_alive = True
        if not manager.is_alive():
            manager.start()
        return manager.submit(fn=fn, args=args, kwargs=kwargs, name=name)

    def _guess_mode(
        self,
        fn: t.Callable[..., t.Any],
        mode: t.Optional[str] = None,
        tags: t.Optional[t.Iterable[str]] = None,
    ) -> t.FrozenSet[str]:
        if tags:
            mode_filter = self._module_specs.filter_by_tags(*tags)
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
