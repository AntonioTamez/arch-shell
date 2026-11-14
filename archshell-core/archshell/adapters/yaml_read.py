"""Safe reader for the restricted YAML subset (AD-11).

Built on PyYAML's `SafeLoader`. Refuses, with `YamlReadError`:

- anchors, aliases, and explicit tags (including `!` and `!!str`);
- merge keys (`<<`) and duplicate mapping keys;
- values outside the subset: anything but `str`, `int`, `bool`, `None`,
  lists, and mappings with `str` keys (so no floats, timestamps, or binary);
- more than one document, and malformed YAML.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from yaml.events import AliasEvent, CollectionStartEvent, ScalarEvent
from yaml.nodes import MappingNode


class YamlReadError(ValueError):
    """A document outside the restricted YAML subset, or not YAML at all."""


_MERGE_TAG = "tag:yaml.org,2002:merge"


class _Loader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):  # noqa: D102 - PyYAML hook
        if not isinstance(node, MappingNode):
            raise YamlReadError(f"expected a mapping at {node.start_mark}")
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            if key_node.tag == _MERGE_TAG:
                raise YamlReadError(f"merge key '<<' is not allowed at {key_node.start_mark}")
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise YamlReadError(f"mapping key must be a string at {key_node.start_mark}")
            if key in mapping:
                raise YamlReadError(f"duplicate mapping key {key!r} at {key_node.start_mark}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _construct_map(loader: _Loader, node):
    return loader.construct_mapping(node, deep=True)


_Loader.add_constructor("tag:yaml.org,2002:map", _construct_map)


def load(text: str) -> object:
    """Parse one YAML document of the restricted subset; an empty text is `None`."""
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, AliasEvent):
                raise YamlReadError(f"alias *{event.anchor} is not allowed at {event.start_mark}")
            if isinstance(event, (ScalarEvent, CollectionStartEvent)):
                if event.anchor is not None:
                    raise YamlReadError(f"anchor &{event.anchor} is not allowed at {event.start_mark}")
                if event.tag is not None:
                    raise YamlReadError(f"tag {event.tag} is not allowed at {event.start_mark}")
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 - _Loader is a SafeLoader
    except yaml.YAMLError as exc:
        raise YamlReadError(f"malformed YAML: {exc}") from None
    except RecursionError:
        raise YamlReadError("document nested too deeply") from None
    _check_subset(data)
    return data


def read(path: Path) -> object:
    """Read `path` as UTF-8 and parse it with `load`."""
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise YamlReadError(f"{path}: not UTF-8: {exc}") from None
    try:
        return load(text)
    except YamlReadError as exc:
        raise YamlReadError(f"{path}: {exc}") from None


def _check_subset(value: object) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, list):
        for item in value:
            _check_subset(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _check_subset(item)
        return
    raise YamlReadError(f"value of type {type(value).__name__} is outside the subset: {value!r}")
