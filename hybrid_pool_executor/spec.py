import importlib
import os
import typing as t

from hybrid_pool_executor.base import ExistsError, ModuleSpec
from hybrid_pool_executor.utils import SingletonMeta


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
        currdir = os.path.dirname(os.path.abspath(__file__))
        package = os.path.basename(currdir)
        package_paths = [os.path.join(currdir, "workers")]
        while package_paths:
            package_path = package_paths.pop(0)
            package_name = package_path[len(currdir) + 1 :].replace(os.sep, ".")
            for item in os.listdir(package_path):
                qualified_path = os.path.join(package_path, item)
                if os.path.isdir(qualified_path):
                    package_paths.append(qualified_path)
                elif os.path.isfile(qualified_path):
                    if not item.endswith(".py"):
                        continue
                    try:
                        if item == "__init__.py":
                            self.import_module(".".join([package, package_name]))
                        else:
                            self.import_module(
                                ".".join([package, package_name, item[:-3]])
                            )
                    except (ImportError, AttributeError):
                        pass

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
