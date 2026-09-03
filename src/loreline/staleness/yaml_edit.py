"""Rewriting single values in a YAML file without disturbing anything else.

capabilities.yaml is not a data dump, it is documentation that happens to
parse: roughly a third of its lines are comments recording vendor doc
conflicts, why a value is deliberately conservative, what a maintainer has to
check before unhiding a model, and the doc URL each claim came from. Those
comments are the reason the file can be trusted, and losing them would cost far
more than the hand editing this module exists to replace.

WHY NOT A ROUND-TRIP LIBRARY
============================

The obvious tool is ruamel.yaml, which parses to a mutable tree and can emit
comments again. It was not used, for three reasons:

* It preserves comments, but not the file. It re-emits every line from its own
  model, so line width, flow versus block style, quoting, blank-line runs and
  the alignment of trailing comments are all decided by the emitter's settings
  rather than by what the file says today. Getting a 1400 line hand-formatted
  document back out byte-identical means tuning emitter options until it
  happens to match, and every future hand edit can break that agreement again.
* It is a new runtime dependency for a maintenance script.
* It rewrites the whole file even when nothing changed, so "no drift" and
  "silently reformatted the file" look the same in git.

This module takes the other option: leave the file alone and splice. PyYAML's
``compose()`` returns the node graph with a start and end mark for every node,
giving the exact byte span each value occupies in the source. Replacing that
span and nothing else means:

* every byte outside an edited value is preserved by construction, comments
  included, so "no drift" is provably a no-op rather than a re-render that
  happened to agree;
* the diff a maintainer reviews is one line per changed value;
* a trailing comment on the same line survives, because it sits outside the
  value's span.

WHAT IT REFUSES TO DO
=====================

The splice is only safe where the span is exactly the value. Two cases are
therefore not attempted, and the caller reports them for a human instead of
guessing:

* Inserting a key that is not in the file. There is no span to replace, and the
  right place for a new line is a judgement about the comments around it.
* Replacing a span that contains a ``#``. That is a block-style collection with
  comments inside it (the Gemini reasoning blocks are the live example), and
  re-rendering it would delete them. Block sequences are refused for a second
  reason as well: PyYAML's end mark for one runs past the last item into the
  following whitespace, so the span is not the value.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import cast

import yaml


@dataclass(frozen=True, slots=True)
class ById:
    """Path step selecting the sequence entry whose ``id`` key matches.

    Models are addressed by id rather than by list position so that reordering
    the curated list, which is a thing maintainers do, cannot make this script
    write a value onto the wrong model.
    """

    value: str

    def __str__(self) -> str:
        return f"id={self.value}"


Step = str | ById
Path = tuple[Step, ...]


def show_path(path: Path) -> str:
    return ".".join(str(step) for step in path)


@dataclass(frozen=True, slots=True)
class Located:
    """One value found in the source, with the byte span it occupies."""

    path: Path
    node: yaml.Node
    start: int
    end: int
    source: str

    @property
    def is_flow(self) -> bool:
        """Whether the value is written inline (``[a, b]``, ``{k: v}``).

        Only flow collections may be replaced wholesale: their span is exactly
        the value, and the file never puts a comment inside one.
        """
        if isinstance(self.node, yaml.ScalarNode):
            return True
        return bool(getattr(self.node, "flow_style", False))

    @property
    def carries_comment(self) -> bool:
        return "#" in self.source


class SpliceRefusedError(Exception):
    """This value cannot be rewritten in place without losing something."""


class YamlDocument:
    """A YAML file addressed by path, edited by byte-span replacement.

    Edits are staged and applied in one pass from the end of the file
    backwards, so that an earlier splice cannot invalidate a later span's
    offsets.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        root = _compose(text)
        if root is None:
            raise ValueError("capabilities.yaml is empty")
        self._root: yaml.Node = root
        self._edits: dict[tuple[int, int], str] = {}

    @property
    def text(self) -> str:
        """The current source, staged edits included."""
        if not self._edits:
            return self._text
        out = self._text
        for (start, end), replacement in sorted(self._edits.items(), reverse=True):
            out = out[:start] + replacement + out[end:]
        return out

    @property
    def edited(self) -> bool:
        return bool(self._edits)

    def locate(self, path: Path) -> Located | None:
        """The value at ``path``, or None if the file does not carry it.

        None is the "no line to replace" case and is never an error here: the
        caller decides whether a missing key is a fact worth reporting or a
        key the file deliberately omits.
        """
        node: yaml.Node = self._root
        for step in path:
            found = _child(node, step)
            if found is None:
                return None
            node = found
        start = int(node.start_mark.index)
        end = int(node.end_mark.index)
        return Located(path, node, start, end, self._text[start:end])

    def replace(self, located: Located, rendered: str) -> None:
        """Stage a replacement of one value's span.

        Raises :class:`SpliceRefusedError` rather than writing when the span is not
        exactly the value: a block collection, or one carrying a comment.
        """
        if not located.is_flow:
            raise SpliceRefusedError(
                f"{show_path(located.path)} is written as a block, "
                "which cannot be replaced without re-rendering its lines"
            )
        if located.carries_comment:
            raise SpliceRefusedError(
                f"{show_path(located.path)} carries a comment inside the value, "
                "which a rewrite would delete"
            )
        self._edits[located.start, located.end] = rendered


def _compose(text: str) -> yaml.Node | None:
    """The node graph for a document, with the marks this module edits by.

    Wrapped rather than called inline because PyYAML ships no type
    information: ``compose`` is Unknown to the type checker, and confining that
    to one line keeps the rest of the module typed.
    """
    composed: object = yaml.compose(text)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return cast("yaml.Node | None", composed)


def _child(node: yaml.Node, step: Step) -> yaml.Node | None:
    if isinstance(step, ById):
        if not isinstance(node, yaml.SequenceNode):
            return None
        for entry in cast("list[yaml.Node]", node.value):
            key = _child(entry, "id")
            if isinstance(key, yaml.ScalarNode) and key.value == step.value:
                return entry
        return None
    if not isinstance(node, yaml.MappingNode):
        return None
    pairs = cast("list[tuple[yaml.Node, yaml.Node]]", node.value)
    for key, value in pairs:
        if isinstance(key, yaml.ScalarNode) and key.value == step:
            return value
    return None


# --------------------------------------------------------------------------
# Rendering values back into the file's own style
# --------------------------------------------------------------------------

# Characters that make a plain (unquoted) scalar ambiguous or illegal inside a
# flow collection. The aspect ratios are the reason this matters: bare 16:9
# parses as a single-pair mapping, not as the string the file means.
_NEEDS_QUOTES = set(":,[]{}#&*!|>'\"%@`")


def _quote_style(node: yaml.Node | None) -> str | None:
    """The quoting the file already uses for the strings in a collection.

    Read from the source rather than chosen, so a rewritten list keeps looking
    like its neighbours: the resolutions are double quoted, the reasoning
    efforts are bare, and both are correct as written.
    """
    if isinstance(node, yaml.ScalarNode):
        return cast("str | None", node.style)
    if isinstance(node, yaml.SequenceNode):
        for item in cast("list[yaml.Node]", node.value):
            style = _quote_style(item)
            if style is not None:
                return style
    return None


def render_scalar(value: object, *, quote: str | None = None) -> str:
    """One scalar as the file would write it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return "null"
    text = str(value)
    if quote == "'":
        return "'" + text.replace("'", "''") + "'"
    if quote == '"' or not text or set(text) & _NEEDS_QUOTES:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def render_sequence(values: Iterable[object], *, like: yaml.Node | None = None) -> str:
    """A flow sequence, quoting its items the way the existing one does."""
    quote = _quote_style(like)
    return "[" + ", ".join(render_scalar(v, quote=quote) for v in values) + "]"


def render_mapping(pairs: Sequence[tuple[str, object]], *, like: yaml.Node | None = None) -> str:
    """A flow mapping, for the one block that is written inline as a unit."""
    del like  # keys are bare in this file; kept for symmetry with the others.
    parts: list[str] = []
    for key, value in pairs:
        rendered = (
            render_sequence(cast("Sequence[object]", value))
            if isinstance(value, list | tuple)
            else render_scalar(value)
        )
        parts.append(f"{key}: {rendered}")
    return "{" + ", ".join(parts) + "}"


__all__ = [
    "ById",
    "Located",
    "Path",
    "SpliceRefusedError",
    "Step",
    "YamlDocument",
    "render_mapping",
    "render_scalar",
    "render_sequence",
    "show_path",
]
