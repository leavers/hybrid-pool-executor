import atexit
import os
import importlib
import inspect
import weakref
from functools import lru_cache
from typing import Any, cast, Callable, Dict, Optional, Tuple
from hybrid_pool_executor.base import (
    BaseExecutor,
    BaseManagerSpec,
    BaseManager,
    Future,
    ModuleSpec,
)

_all_executors = weakref.WeakSet()


@atexit.register
def _python_exit():
    for executor in _all_executors:
        if executor.is_alive():
            executor.shutdown()


@lru_cache(maxsize=1)
def _get_module_specs() -> Dict[str, ModuleSpec]:
    package_name = "workers"
    package_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), package_name
    )
    specs: Dict[str, ModuleSpec] = {}
    for item in os.listdir(package_path):
        if item.startswith("_") or not item.endswith(".py"):
            continue
        if not os.path.isfile(os.path.join(package_path, item)):
            continue
        try:
            module_spec: ModuleSpec = cast(
                ModuleSpec,
                importlib.import_module(
                    ".".join(["hybrid_pool_executor", package_name, item[:-3]])
                ).MODULE_SPEC,
            )
            if (name := module_spec.name) in specs:
                raise ValueError(f'Found duplicated spec "{name}".')
            specs[module_spec.name] = module_spec
        except ImportError:
            pass
    return specs


class HybridPoolExecutor(BaseExecutor):
    def __init__(
        self,
        thread_workers: int = -1,
        incremental_thread_workers: bool = True,
        thread_worker_name_pattern: Optional[str] = None,
        redirect_thread: Optional[str] = None,
        **kwargs,
    ):
        self._module_specs: Dict[str, ModuleSpec] = _get_module_specs()
        self._managers: Dict[str, BaseManager] = {}
        self._manager_kwargs = {
            "thread_workers": thread_workers,
            "incremental_thread_workers": incremental_thread_workers,
            "thread_worker_name_pattern": thread_worker_name_pattern,
            "redirect_thread": redirect_thread,
            **kwargs,
        }
        # ensure basic managers are available
        basic_modes = ("thread",)
        for mode in basic_modes:
            if mode not in self._module_specs:
                raise AssertionError(
                    f'Manager "{mode}" is required for HybridPoolExecutor '
                    "but not found."
                )
        global _all_executors
        _all_executors.add(self)
        self._is_alive: bool = True

    @classmethod
    def _get_manager(
        cls, mode: str, module_spec: ModuleSpec, kwargs: Dict[str, Any]
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

    def submit(self, fn: Callable[..., Any], /, *args, **kwargs) -> Future:
        mode = kwargs.pop("_mode", None)
        return self.submit_task(fn, args=args, kwargs=kwargs, mode=mode)

    def submit_task(
        self,
        fn: Callable[..., Any],
        args: Optional[Tuple[Any, ...]] = (),
        kwargs: Optional[Dict[str, Any]] = None,
        name: Optional[str] = None,
        mode: str = None,
    ) -> Future:
        if not mode:
            mode = "async" if inspect.iscoroutinefunction(fn) else "thread"
        if mode not in self._module_specs:
            raise NotImplementedError(f'Mode "{mode}" is not supported.')
        self._is_alive = True
        if mode not in self._managers:
            module_spec: ModuleSpec = self._module_specs[mode]
            manager = self._get_manager(
                mode=mode,
                module_spec=module_spec,
                kwargs=self._manager_kwargs,
            )
            manager.start()
            self._managers[mode] = manager
        manager: BaseManager = self._managers[mode]
        return manager.submit(fn=fn, args=args, kwargs=kwargs, name=name)

    def is_alive(self) -> bool:
        return self._is_alive

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False):
        for manager in self._managers.values():
            if wait:
                manager.stop()
            else:
                manager.terminate()
        self._is_alive = False
