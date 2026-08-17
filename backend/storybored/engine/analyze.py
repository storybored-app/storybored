# OWNED-BY: engine-agent
"""Workflow-graph analysis for the in-app import wizard.

Given a ComfyUI **API-format** graph, propose a manifest draft: which node
holds the prompt, the seed, the canvas size, the output, the swappable model
loaders and the LoRA splice seam. Every detection is a *suggestion* carrying
the node id and a confidence — ambiguity (two text encodes, no save node)
surfaces as candidate lists plus a warning, never an error, so the wizard can
ask the user instead of guessing silently.

Pure functions, no I/O, no ComfyUI. Reuses the validate-pack derivation for
``required_models`` so the wizard and the CLI can never disagree.
"""

from typing import Any

from storybored.engine.graph import lora_chain
from storybored.engine.validate import derive_required_models

#: node classes whose presence marks a graph as a video workflow
VIDEO_CLASSES = {
    "SaveVideo",
    "CreateVideo",
    "SaveWEBM",
    "VHS_VideoCombine",
}

#: output-sink classes beyond the "class name contains Save" rule
_EXTRA_SINK_CLASSES = {"VHS_VideoCombine"}

#: model loaders users may want as a swappable slot: class → (input, slot key)
MODEL_SLOT_LOADERS = {
    "UNETLoader": ("unet_name", "unet"),
    "CheckpointLoaderSimple": ("ckpt_name", "ckpt"),
}

#: input names that carry the clip length on video sampler/latent nodes
_LENGTH_INPUTS = ("length", "frames", "num_frames")


def is_ui_format(graph: Any) -> bool:
    """True for ComfyUI's editor-format export (the plain "Save" file)."""
    return isinstance(graph, dict) and ("nodes" in graph or "links" in graph)


def is_api_format(graph: Any) -> bool:
    """True for a non-empty API-format graph (node ids → objects w/ class_type)."""
    return (
        isinstance(graph, dict)
        and bool(graph)
        and all(isinstance(n, dict) and "class_type" in n for n in graph.values())
    )


def _inputs(node: dict) -> dict:
    return node.get("inputs") or {}


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2


def _node_ids(graph: dict) -> list[str]:
    return sorted(graph, key=str)


def _first_upstream(
    graph: dict, start: Any, targets: set[str], max_hops: int = 20
) -> str | None:
    """Follow links upstream (breadth-first) from a link value to the first
    node whose id is in ``targets``. Used to trace a sampler's positive /
    negative conditioning back through reroutes/ConditioningZeroOut to the
    text encode that feeds it."""
    if not _is_link(start):
        return None
    queue = [str(start[0])]
    seen: set[str] = set()
    while queue and max_hops > 0:
        max_hops -= 1
        node_id = queue.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        if node_id in targets:
            return node_id
        for value in _inputs(graph.get(node_id) or {}).values():
            if _is_link(value):
                queue.append(str(value[0]))
    return None


def _prompt_candidates(graph: dict, warnings: list[str]) -> list[dict]:
    """Text inputs that can hold the shot prompt.

    Two families: text-encode nodes (``text`` input, class contains
    "TextEncode") and nodes with a free string ``prompt`` input (video
    image-to-video nodes carry the prompt themselves). Role positive/negative
    is resolved by tracing each sampler's conditioning inputs upstream.
    """
    candidates: list[dict] = []
    by_node: dict[str, dict] = {}
    for node_id in _node_ids(graph):
        node = graph[node_id]
        class_type = str(node.get("class_type", ""))
        for input_name in ("text", "prompt"):
            value = _inputs(node).get(input_name)
            if not isinstance(value, str):
                continue
            if input_name == "text" and "TextEncode" not in class_type:
                continue
            cand = {
                "node": node_id,
                "input": input_name,
                "class_type": class_type,
                "preview": value[:120],
                "role": None,
            }
            candidates.append(cand)
            by_node[node_id] = cand
            break  # one prompt input per node

    # Resolve roles from sampler conditioning links (positive claims first).
    targets = set(by_node)
    for role in ("positive", "negative"):
        for node_id in _node_ids(graph):
            link = _inputs(graph[node_id]).get(role)
            found = _first_upstream(graph, link, targets)
            if found is not None and by_node[found]["role"] is None:
                by_node[found]["role"] = role

    positives = [c for c in candidates if c["role"] == "positive"]
    if len(candidates) > 1 and not positives:
        warnings.append(
            "several text inputs found and none is clearly the positive prompt "
            "— confirm which one receives the shot description"
        )
    return candidates


def _pick_prompt(candidates: list[dict]) -> dict | None:
    positives = [c for c in candidates if c["role"] == "positive"]
    if len(positives) == 1:
        return {**_ref(positives[0]), "confidence": "high"}
    if len(candidates) == 1:
        return {**_ref(candidates[0]), "confidence": "high"}
    if positives:
        return {**_ref(positives[0]), "confidence": "medium"}
    if candidates:
        return {**_ref(candidates[0]), "confidence": "low"}
    return None


def _ref(candidate: dict) -> dict:
    ref = {"node": candidate["node"]}
    if "input" in candidate:
        ref["input"] = candidate["input"]
    return ref


def _seed_candidates(graph: dict) -> list[dict]:
    out = []
    for node_id in _node_ids(graph):
        node = graph[node_id]
        for input_name in ("seed", "noise_seed"):
            value = _inputs(node).get(input_name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append(
                    {
                        "node": node_id,
                        "input": input_name,
                        "class_type": str(node.get("class_type", "")),
                        "value": value,
                    }
                )
                break
    return out


def _size_candidates(graph: dict) -> list[dict]:
    """Nodes with literal width+height inputs (empty-latent family, video
    samplers, ModelSamplingFlux-style nodes)."""
    out = []
    for node_id in _node_ids(graph):
        node = graph[node_id]
        inputs = _inputs(node)
        w, h = inputs.get("width"), inputs.get("height")
        if isinstance(w, (int, float)) and isinstance(h, (int, float)):
            out.append(
                {
                    "node": node_id,
                    "class_type": str(node.get("class_type", "")),
                    "width": w,
                    "height": h,
                }
            )
    return out


def _pick_size(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    latents = [c for c in candidates if "LatentImage" in c["class_type"]]
    if len(candidates) == 1:
        return {"node": candidates[0]["node"], "confidence": "high"}
    if len(latents) == 1:
        return {"node": latents[0]["node"], "confidence": "medium"}
    return {"node": candidates[0]["node"], "confidence": "low"}


def _output_candidates(graph: dict) -> list[dict]:
    out = []
    for node_id in _node_ids(graph):
        node = graph[node_id]
        class_type = str(node.get("class_type", ""))
        if "Save" in class_type or class_type in _EXTRA_SINK_CLASSES:
            out.append({"node": node_id, "class_type": class_type})
    return out


def _single_or_first(candidates: list[dict], warnings: list[str], what: str) -> dict | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return {**_ref(candidates[0]), "confidence": "high"}
    warnings.append(f"several {what} nodes found — confirm which one to use")
    return {**_ref(candidates[0]), "confidence": "medium"}


def _model_slots(graph: dict) -> list[dict]:
    slots: list[dict] = []
    used_keys: set[str] = set()
    for node_id in _node_ids(graph):
        node = graph[node_id]
        class_type = str(node.get("class_type", ""))
        spec = MODEL_SLOT_LOADERS.get(class_type)
        if spec is None:
            continue
        input_name, base_key = spec
        value = _inputs(node).get(input_name)
        if not isinstance(value, str):
            continue
        key, n = base_key, 2
        while key in used_keys:
            key, n = f"{base_key}_{n}", n + 1
        used_keys.add(key)
        slots.append(
            {
                "key": key,
                "node": node_id,
                "input": input_name,
                "class_type": class_type,
                "value": value,
            }
        )
    return slots


def _seam(graph: dict, kind: str, warnings: list[str]) -> dict:
    """Where runtime LoRAs (characters, styles, user additions) splice in:
    the last LoRA loader in the model chain, else the model loader itself."""
    loaders = [
        node_id
        for node_id in _node_ids(graph)
        if str(graph[node_id].get("class_type", "")) in MODEL_SLOT_LOADERS
    ]
    chain = lora_chain(graph)
    candidates = [
        {
            "node": node_id,
            "class_type": str(graph[node_id].get("class_type", "")),
            "lora_name": str(_inputs(graph[node_id]).get("lora_name", "")) or None,
        }
        for node_id in loaders + chain
    ]
    field = "character_injection" if kind == "image" else "lora_injection"
    # video LoRAs take over only the model path — CLIP rarely routes through
    # the chain in video graphs (see the shipped minimax pack)
    class_type = "LoraLoaderModelOnly" if kind == "video" else None
    suggested: dict | None = None
    if chain:
        suggested = {"after_node": chain[-1], "confidence": "high"}
    elif len(loaders) == 1:
        suggested = {"after_node": loaders[0], "confidence": "medium"}
    elif loaders:
        warnings.append(
            "several model loaders found — confirm which one extra styles and "
            "characters should attach to"
        )
        suggested = {"after_node": loaders[0], "confidence": "low"}
    if suggested is not None:
        suggested["field"] = field
        if class_type:
            suggested["class_type"] = class_type
    return {"candidates": candidates, "suggested": suggested}


def analyze_graph(graph: dict) -> dict:
    """The full manifest draft for one API-format graph (see module docstring)."""
    warnings: list[str] = []
    classes = {str(n.get("class_type", "")) for n in graph.values()}
    kind = "video" if classes & VIDEO_CLASSES else "image"

    prompt_cands = _prompt_candidates(graph, warnings)
    if not prompt_cands:
        warnings.append(
            "no text prompt found — the graph needs a text input StoryBored "
            "can write the shot description into"
        )
    seed_cands = _seed_candidates(graph)
    if len(seed_cands) > 1:
        warnings.append("several seed inputs found — confirm which one to randomize")
    size_cands = _size_candidates(graph)
    output_cands = _output_candidates(graph)
    if not output_cands:
        warnings.append(
            "no save node found — add a SaveImage/SaveVideo node and re-export"
        )
    image_cands = [
        {
            "node": node_id,
            "input": "image",
            "class_type": str(graph[node_id].get("class_type", "")),
        }
        for node_id in _node_ids(graph)
        if str(graph[node_id].get("class_type", "")) == "LoadImage"
        and isinstance(_inputs(graph[node_id]).get("image"), str)
    ]

    roles: dict[str, dict] = {
        "prompt": {"candidates": prompt_cands, "suggested": _pick_prompt(prompt_cands)},
        "seed": {
            "candidates": seed_cands,
            "suggested": (
                {**_ref(seed_cands[0]), "confidence": "high" if len(seed_cands) == 1 else "medium"}
                if seed_cands
                else None
            ),
        },
        "size": {"candidates": size_cands, "suggested": _pick_size(size_cands)},
        "output": {
            "candidates": output_cands,
            "suggested": _single_or_first(output_cands, warnings, "save"),
        },
        "image": {
            "candidates": image_cands,
            "suggested": (
                {**_ref(image_cands[0]), "confidence": "high"}
                if kind == "video" and image_cands
                else None
            ),
        },
        "seam": _seam(graph, kind, warnings),
    }

    length_cands: list[dict] = []
    frame_conditioning: dict | None = None
    if kind == "video":
        for node_id in _node_ids(graph):
            node = graph[node_id]
            for input_name in _LENGTH_INPUTS:
                value = _inputs(node).get(input_name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    length_cands.append(
                        {
                            "node": node_id,
                            "input": input_name,
                            "class_type": str(node.get("class_type", "")),
                            "value": value,
                        }
                    )
                    break
        if not image_cands:
            warnings.append(
                "no image-input node found — a video engine needs a LoadImage "
                "node to receive the shot's approved still"
            )
        for node_id in _node_ids(graph):
            if "first_frame" in _inputs(graph[node_id]):
                frame_conditioning = {
                    "node": node_id,
                    "first": "first_frame",
                    "last": "last_frame",
                }
                break
    roles["length"] = {
        "candidates": length_cands,
        "suggested": (
            {**_ref(length_cands[0]), "confidence": "high" if len(length_cands) == 1 else "medium"}
            if length_cands
            else None
        ),
    }

    return {
        "kind": kind,
        "node_count": len(graph),
        "roles": roles,
        "model_slots": _model_slots(graph),
        "frame_conditioning": frame_conditioning,
        "required_models": derive_required_models(graph),
        "warnings": warnings,
    }
