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

#: LoRA loader classes we splice/override. Model-only loaders have no clip
#: path (used by video packs, whose CLIP never routes through the LoRA chain).
_LORA_CLASSES = {"LoraLoader", "LoraLoaderModelOnly"}
_MODEL_ONLY_LORA = "LoraLoaderModelOnly"


def lora_injection_spec(manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Where extra LoRAs splice into this pack's graph.

    ``lora_injection`` ({"after_node", "class_type"?}) wins when present;
    otherwise the pack's ``character_injection`` doubles as the splice point
    (class defaults to LoraLoader either way).
    """
    return manifest.get("lora_injection") or manifest.get("character_injection")


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
    graph: dict,
    tail: str,
    id_prefix: str,
    entries: Sequence[tuple[str, float]],
    class_type: str = "LoraLoader",
) -> list[str]:
    """Splice a chain of LoRA loaders after ``tail`` (mutates the graph).

    Per the contract: each new node's model/clip inputs take the current tail
    node's outputs 0/1, then every OTHER node that referenced
    ``[tail, 0]``/``[tail, 1]`` is rewired to the new node. Entries chain in
    sequence. Model-only loaders carry no clip path, so only output 0 is
    taken over. Returns the injected node ids (in chain order).
    """
    model_only = class_type == _MODEL_ONLY_LORA
    outputs = (0,) if model_only else (0, 1)
    injected: list[str] = []
    for i, (lora_name, strength) in enumerate(entries):
        node_id = f"{id_prefix}{i}"
        while node_id in graph:
            node_id += "_"
        # Rewire all existing referents of the current tail's spliced outputs.
        for other in graph.values():
            inputs = other.get("inputs") or {}
            for name, value in inputs.items():
                if (
                    isinstance(value, list)
                    and len(value) == 2
                    and value[0] == tail
                    and value[1] in outputs
                ):
                    inputs[name] = [node_id, value[1]]
        new_inputs: dict[str, Any] = {
            "lora_name": lora_name,
            "strength_model": strength,
            "model": [tail, 0],
        }
        if not model_only:
            new_inputs["strength_clip"] = strength
            new_inputs["clip"] = [tail, 1]
        graph[node_id] = {"class_type": class_type, "inputs": new_inputs}
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
        raise ValueError(f"lora injection after_node '{tail}' is not in the graph")
    return _splice_lora_chain(
        graph, tail, id_prefix, entries,
        class_type=str(injection.get("class_type") or "LoraLoader"),
    )


def lora_chain(graph: Mapping[str, Any]) -> list[str]:
    """LoRA loader node ids in chain order (base model first).

    Order = depth following each node's ``model`` link through other
    LoRA loaders; nodes fed straight from a loader come first.
    """
    loras = {k for k, v in graph.items() if v.get("class_type") in _LORA_CLASSES}

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
        if node is None or node.get("class_type") not in _LORA_CLASSES:
            continue
        model_only = node.get("class_type") == _MODEL_ONLY_LORA
        inputs = node.setdefault("inputs", {})
        if entry.get("enabled", True) is False:
            inputs["strength_model"] = 0
            if not model_only:
                inputs["strength_clip"] = 0
        elif "strength" in entry:
            strength = float(entry["strength"])
            inputs["strength_model"] = strength
            if not model_only:
                inputs["strength_clip"] = strength


def added_engine_loras(entries: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The enabled append-entries (have lora_name, no node) from parse_engine_loras."""
    return [
        {"lora_name": e["lora_name"], "strength": float(e.get("strength", 1.0))}
        for e in entries
        if e.get("lora_name") and not e.get("node") and e.get("enabled", True) is not False
    ]


def parse_engine_models(raw: str) -> dict[str, dict[str, str]]:
    """Parse the ``engine_models`` setting (JSON string): pack id → slot key → file.

    Defensive like the LoRA parsers — malformed data is dropped, never fatal.
    """
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for pack_id, slots in data.items():
        if not isinstance(slots, dict):
            continue
        clean = {
            str(key): str(name).strip()
            for key, name in slots.items()
            if isinstance(name, str) and name.strip()
        }
        if clean:
            result[str(pack_id)] = clean
    return result


def apply_model_overrides(
    graph: dict, manifest: Mapping[str, Any], overrides: Mapping[str, str]
) -> None:
    """Write user-chosen model files into the manifest's model slots (mutates).

    ``overrides`` maps slot key → filename (from the engine_models setting).
    Unknown slot keys and slots without an override are left alone, so a stale
    setting can never sink a render after a pack update.
    """
    for slot in manifest.get("model_slots") or []:
        key = str(slot.get("key", ""))
        value = overrides.get(key)
        if not value:
            continue
        node = graph.get(str(slot.get("node", "")))
        input_name = str(slot.get("input", ""))
        if node is None or not input_name:
            continue
        node.setdefault("inputs", {})[input_name] = value


def set_frame_position(graph: dict, manifest: Mapping[str, Any], position: str) -> None:
    """Anchor the conditioning still as the clip's first or last frame (mutates).

    The manifest's ``frame_conditioning`` names the sampler node and its two
    image inputs; "last" moves whatever feeds the first-frame input onto the
    last-frame input. Raises ValueError when "last" is requested but the pack
    doesn't declare last-frame support.
    """
    if position != "last":
        return
    spec = manifest.get("frame_conditioning")
    if not spec:
        raise ValueError("this video engine can't use the still as the last frame")
    node = graph.get(str(spec.get("node", "")))
    first_input = str(spec.get("first", "first_frame"))
    last_input = str(spec.get("last", "last_frame"))
    if node is None or first_input not in (node.get("inputs") or {}):
        raise ValueError(
            f"frame_conditioning node '{spec.get('node')}' has no '{first_input}' input"
        )
    node["inputs"][last_input] = node["inputs"].pop(first_input)


def set_filename_prefix(graph: dict, prefix: str) -> None:
    """Point every SaveImage/SaveVideo node at our unambiguous output prefix."""
    for node in graph.values():
        if node.get("class_type") in _SAVE_CLASSES:
            node.setdefault("inputs", {})["filename_prefix"] = prefix
