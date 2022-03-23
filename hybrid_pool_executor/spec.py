import importlib
import typing as t

from hybrid_pool_executor.base import ExistsError, ModuleSpec
from hybrid_pool_executor.utils import SingletonMeta

_default_modules = [
    "hybrid_pool_executor.workers.async_",
    "hybrid_pool_executor.workers.process",
    "hybrid_pool_executor.workers.thread",
]


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
        for module in _default_modules:
            self.import_module(module)

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


spec_factory = ModuleSpecFactory()
