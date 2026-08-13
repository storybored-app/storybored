# OWNED-BY: engine-agent
"""Graph transforms: manifest parameter application, @handle mention parsing,
character LoRA injection and output filename prefixing.

All functions operate on ComfyUI **API-format** graphs:
``{node_id: {"class_type": str, "inputs": {name: value | [src_node_id, output_idx]}}}``.
"""

import copy
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

#: @handle mentions in shot descriptions (handles are stored lowercase, no @).
#: handles are stored lowercase, but users (and LLMs) type "@Nova" — match
#: case-insensitively and normalize at lookup so the LoRA still injects
MENTION_RE = re.compile(r"@([A-Za-z0-9_-]+)")

#: manifest parameter type → python coercion applied before writing into the graph
_COERCERS = {"int": int, "seed": int, "float": float}

#: node classes whose filename_prefix identifies our outputs in /history
_SAVE_CLASSES = {"SaveImage", "SaveVideo"}


def parse_mentions(text: str) -> list[str]:
    """Ordered, de-duplicated @handles found in the text."""
    seen: set[str] = set()
    handles: list[str] = []
    for handle in MENTION_RE.findall(text or ""):
        handle = handle.lower()
        if handle not in seen:
            seen.add(handle)
            handles.append(handle)
    return handles


def substitute_mentions(text: str, characters: Mapping[str, Any]) -> str:
    """Replace each known ``@handle`` with ``"{trigger} {class_word}"``.

    `characters` maps handle → object with .trigger and .class_word.
    Unknown handles are left untouched.
    """

    def repl(match: re.Match) -> str:
        char = characters.get(match.group(1).lower())
        if char is None:
            return match.group(0)
        return f"{char.trigger} {char.class_word}".strip()

    return MENTION_RE.sub(repl, text or "")


def apply_parameters(graph: dict, manifest: dict, params: Mapping[str, Any]) -> dict:
    """Deep-copy the graph and write manifest parameters into it.

    For each manifest parameter: use the caller's value when present, else the
    manifest default, else leave the graph's baked-in value alone.
    """
    result = copy.deepcopy(graph)
    for spec in manifest.get("parameters") or []:
        key = spec.get("key")
        node_id = str(spec.get("node", ""))
        input_name = spec.get("input")
        if not key or not node_id or not input_name:
            continue
        if key in params and params[key] is not None:
            value = params[key]
        elif "default" in spec:
            value = spec["default"]
        else:
            continue
        coerce = _COERCERS.get(spec.get("type", ""))
        if coerce is not None:
            value = coerce(value)
        node = result.get(node_id)
        if node is None:
            raise ValueError(
                f"manifest parameter '{key}' targets unknown graph node '{node_id}'"
            )
        node.setdefault("inputs", {})[input_name] = value
    return result


def _splice_lora_chain(
    graph: dict, tail: str, id_prefix: str, entries: Sequence[tuple[str, float]]
) -> list[str]:
    """Splice a chain of LoraLoaders after ``tail`` (mutates the graph).

    Per the contract: each new node's model/clip inputs take the current tail
    node's outputs 0/1, then every OTHER node that referenced
    ``[tail, 0]``/``[tail, 1]`` is rewired to the new node. Entries chain in
    sequence. Returns the injected node ids (in chain order).
    """
    injected: list[str] = []
    for i, (lora_name, strength) in enumerate(entries):
        node_id = f"{id_prefix}{i}"
        while node_id in graph:
            node_id += "_"
        # Rewire all existing referents of the current tail's outputs 0/1.
        for other in graph.values():
            inputs = other.get("inputs") or {}
            for name, value in inputs.items():
                if (
                    isinstance(value, list)
                    and len(value) == 2
                    and value[0] == tail
                    and value[1] in (0, 1)
                ):
                    inputs[name] = [node_id, value[1]]
        graph[node_id] = {
            "class_type": "LoraLoader",
            "inputs": {
                "lora_name": lora_name,
                "strength_model": strength,
                "strength_clip": strength,
                "model": [tail, 0],
                "clip": [tail, 1],
            },
        }
        injected.append(node_id)
        tail = node_id
    return injected


def inject_characters(
    graph: dict, injection: Mapping[str, Any] | None, characters: Sequence[Any]
) -> list[str]:
    """Splice one LoraLoader per character into the graph (mutates it).

    While at least one character LoRA is active, every node in
    ``disable_nodes`` gets strength_model/strength_clip zeroed.

    Returns the injected node ids (in chain order).
    """
    active = [c for c in characters if getattr(c, "lora_name", "")]
    if injection is None or not active:
        return []
    tail = str(injection.get("after_node", ""))
    if tail not in graph:
        raise ValueError(f"character_injection.after_node '{tail}' is not in the graph")

    injected = _splice_lora_chain(
        graph,
        tail,
        "char_lora_",
        [(c.lora_name, float(getattr(c, "lora_strength", 1.0))) for c in active],
    )

    for node_id in injection.get("disable_nodes") or []:
        node = graph.get(str(node_id))
        if node is not None:
            node.setdefault("inputs", {})["strength_model"] = 0
            node["inputs"]["strength_clip"] = 0
    return injected


def parse_style_loras(raw: str) -> list[dict]:
    """Parse the ``style_loras`` setting (JSON string) into enabled entries.

    Returns ``[{"lora_name": str, "strength": float}, ...]`` keeping only
    enabled entries with a lora_name. Malformed JSON or entries yield [] /
    are skipped — a bad setting must never sink a render.
    """
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    entries: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("lora_name", "")).strip()
        if not name or item.get("enabled", True) is False:
            continue
        try:
            strength = float(item.get("strength", 1.0))
        except (TypeError, ValueError):
            strength = 1.0
        entries.append({"lora_name": name, "strength": strength})
    return entries


def inject_style_loras(
    graph: dict, injection: Mapping[str, Any] | None, styles: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Splice style LoRAs right after the pack's injection point (mutates it).

    ``styles`` comes from parse_style_loras. Style LoRAs reuse the pack's
    ``character_injection.after_node`` splice point; when called after
    inject_characters they land between the pack's base stack and the
    character LoRAs, so character identity keeps the final say.

    Returns the injected node ids (in chain order).
    """
    entries = [
        (str(s.get("lora_name", "")), float(s.get("strength", 1.0)))
        for s in styles
        if s.get("lora_name")
    ]
    if injection is None or not entries:
        return []
    tail = str(injection.get("after_node", ""))
    if tail not in graph:
        raise ValueError(f"character_injection.after_node '{tail}' is not in the graph")
    return _splice_lora_chain(graph, tail, "style_lora_", entries)


def set_filename_prefix(graph: dict, prefix: str) -> None:
    """Point every SaveImage/SaveVideo node at our unambiguous output prefix."""
    for node in graph.values():
        if node.get("class_type") in _SAVE_CLASSES:
            node.setdefault("inputs", {})["filename_prefix"] = prefix
