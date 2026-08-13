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
    graph: dict,
    injection: Mapping[str, Any] | None,
    styles: Sequence[Mapping[str, Any]],
    id_prefix: str = "style_lora_",
) -> list[str]:
    """Splice extra LoRAs right after the pack's injection point (mutates it).

    ``styles`` comes from parse_style_loras. Extra LoRAs reuse the pack's
    ``character_injection.after_node`` splice point; each later call lands
    closer to the base stack than earlier ones, so splicing characters, then
    styles, then engine additions yields base → additions → styles →
    characters, keeping character identity last.

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
    return _splice_lora_chain(graph, tail, id_prefix, entries)


def lora_chain(graph: Mapping[str, Any]) -> list[str]:
    """LoraLoader node ids in chain order (base model first).

    Order = depth following each node's ``model`` link through other
    LoraLoaders; nodes fed straight from a loader come first.
    """
    loras = {k for k, v in graph.items() if v.get("class_type") == "LoraLoader"}

    def depth(node_id: str, seen: frozenset = frozenset()) -> int:
        if node_id in seen:  # cycle guard — malformed graph
            return 0
        parent = (graph[node_id].get("inputs") or {}).get("model")
        if isinstance(parent, list) and parent and parent[0] in loras:
            return depth(parent[0], seen | {node_id}) + 1
        return 0

    return sorted(loras, key=lambda n: (depth(n), n))


def parse_engine_loras(raw: str) -> dict[str, list[dict]]:
    """Parse the ``engine_loras`` setting (JSON string): pack id → entries.

    Each entry either overrides a baked LoraLoader node
    (``{"node": id, "strength"?: float, "enabled"?: bool}``) or appends a new
    LoRA (``{"lora_name": name, "strength"?: float, "enabled"?: bool}``).
    Defensive like parse_style_loras — malformed data is dropped, never fatal.
    """
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, list[dict]] = {}
    for pack_id, items in data.items():
        if not isinstance(items, list):
            continue
        entries: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            node = str(item.get("node", "")).strip()
            name = str(item.get("lora_name", "")).strip()
            if not node and not name:
                continue
            try:
                strength = float(item.get("strength", 1.0))
            except (TypeError, ValueError):
                strength = 1.0
            entry: dict = {"strength": strength, "enabled": item.get("enabled", True) is not False}
            if node:
                entry["node"] = node
                if "strength" not in item:
                    entry.pop("strength")  # override may change enabled only
            else:
                entry["lora_name"] = name
            entries.append(entry)
        if entries:
            result[str(pack_id)] = entries
    return result


def apply_engine_lora_overrides(graph: dict, entries: Sequence[Mapping[str, Any]]) -> None:
    """Write per-node strength/enabled overrides onto baked LoraLoaders (mutates).

    ``enabled: false`` zeroes both strengths (LoraLoader skips at 0 — same as
    removing the node). Unknown node ids are ignored so a stale override can
    never sink a render after a pack update.
    """
    for entry in entries:
        node_id = str(entry.get("node", ""))
        if not node_id:
            continue
        node = graph.get(node_id)
        if node is None or node.get("class_type") != "LoraLoader":
            continue
        inputs = node.setdefault("inputs", {})
        if entry.get("enabled", True) is False:
            inputs["strength_model"] = 0
            inputs["strength_clip"] = 0
        elif "strength" in entry:
            strength = float(entry["strength"])
            inputs["strength_model"] = strength
            inputs["strength_clip"] = strength


def added_engine_loras(entries: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The enabled append-entries (have lora_name, no node) from parse_engine_loras."""
    return [
        {"lora_name": e["lora_name"], "strength": float(e.get("strength", 1.0))}
        for e in entries
        if e.get("lora_name") and not e.get("node") and e.get("enabled", True) is not False
    ]


def set_filename_prefix(graph: dict, prefix: str) -> None:
    """Point every SaveImage/SaveVideo node at our unambiguous output prefix."""
    for node in graph.values():
        if node.get("class_type") in _SAVE_CLASSES:
            node.setdefault("inputs", {})["filename_prefix"] = prefix
