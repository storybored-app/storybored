# OWNED-BY: engine-agent
"""Workflow-pack registry.

Packs are folders containing ``manifest.json`` + a ComfyUI API-format graph.
Two locations are scanned: the repo's ``workflows/`` dir and
``DATA_DIR/workflows`` (user-installed packs; same id overrides the repo one).

Availability: the pack's **effective** model set — the manifest's
``required_models`` (``"ClassType.input_name": [filenames]``) with the user's
``engine_models`` slot swaps applied and ``engine_loras``-disabled baked LoRAs
dropped — is checked against the ComfyUI /object_info dropdown enums (cached
60s by the client), and every node class the graph uses must exist on the
engine. Packs with missing models or nodes are flagged, never hidden.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from storybored.config import Settings
from storybored.engine import catalog
from storybored.engine.comfy_client import ComfyClient, ComfyError
from storybored.engine.graph import LORA_CLASSES, lora_chain, lora_injection_spec

log = logging.getLogger("storybored.engine")

#: repo-level workflows dir (…/storybored/workflows)
REPO_WORKFLOWS_DIR = Path(__file__).resolve().parents[3] / "workflows"


@dataclass
class WorkflowPack:
    id: str
    dir: Path
    manifest: dict

    def load_graph(self) -> dict:
        graph_file = self.dir / (self.manifest.get("graph") or "graph.json")
        return json.loads(graph_file.read_text(encoding="utf-8"))


def workflow_dirs(settings: Settings) -> list[Path]:
    return [REPO_WORKFLOWS_DIR, settings.data_path / "workflows"]


def load_packs(settings: Settings) -> dict[str, WorkflowPack]:
    """Scan pack dirs; later dirs (DATA_DIR) override same-id repo packs."""
    packs: dict[str, WorkflowPack] = {}
    for base in workflow_dirs(settings):
        if not base.is_dir():
            continue
        for manifest_path in sorted(base.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("skipping workflow pack %s: %s", manifest_path.parent, exc)
                continue
            pack_id = str(manifest.get("id") or manifest_path.parent.name)
            packs[pack_id] = WorkflowPack(id=pack_id, dir=manifest_path.parent, manifest=manifest)
    return packs


def get_pack(settings: Settings, workflow_id: str) -> WorkflowPack | None:
    return load_packs(settings).get(workflow_id)


def default_workflow_id(packs: dict[str, WorkflowPack], kind: str = "image") -> str | None:
    """Deterministic default: first pack of the kind, sorted by id."""
    ids = sorted(pid for pid, p in packs.items() if p.manifest.get("kind", "image") == kind)
    return ids[0] if ids else None


def effective_required_models(
    pack: WorkflowPack,
    model_overrides: Mapping[str, str] | None = None,
    lora_overrides: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, list[str]]:
    """The manifest's ``required_models`` with the user's settings applied.

    A ``model_slots`` override (``engine_models`` setting) replaces the baked
    filename with the user's choice — a render will load the override, so
    that's the file that must exist. A baked LoRA toggled off via
    ``engine_loras`` renders at strength 0, so its file drops out of the
    required set entirely.
    """
    required = {
        str(spec): list(files or [])
        for spec, files in (pack.manifest.get("required_models") or {}).items()
    }
    if not model_overrides and not lora_overrides:
        return required
    try:
        graph = pack.load_graph()
    except (OSError, json.JSONDecodeError):
        return required

    def drop(spec: str, filename: str) -> None:
        files = required.get(spec)
        if files and filename in files:
            files.remove(filename)

    for slot in pack.manifest.get("model_slots") or []:
        override = (model_overrides or {}).get(str(slot.get("key", "")))
        node = graph.get(str(slot.get("node", "")))
        input_name = str(slot.get("input", ""))
        if not override or node is None or not input_name:
            continue
        spec = f"{node.get('class_type', '')}.{input_name}"
        drop(spec, str((node.get("inputs") or {}).get(input_name, "")))
        files = required.setdefault(spec, [])
        if override not in files:
            files.append(override)

    for entry in lora_overrides or []:
        if entry.get("enabled", True) is not False:
            continue
        node = graph.get(str(entry.get("node", "")))
        if node is None or node.get("class_type") not in LORA_CLASSES:
            continue
        spec = f"{node.get('class_type', '')}.lora_name"
        drop(spec, str((node.get("inputs") or {}).get("lora_name", "")))
    return required


def required_node_classes(pack: WorkflowPack) -> list[str]:
    """Every node class the pack needs on the engine, derived from the graph.

    The manifest may list extra classes under ``required_nodes`` (e.g. classes
    a custom node creates at runtime); they're unioned in.
    """
    classes = {str(c) for c in pack.manifest.get("required_nodes") or []}
    try:
        graph = pack.load_graph()
    except (OSError, json.JSONDecodeError):
        return sorted(c for c in classes if c)
    classes.update(
        str(node.get("class_type", ""))
        for node in graph.values()
        if isinstance(node, dict)
    )
    return sorted(c for c in classes if c)


async def pack_availability(
    pack: WorkflowPack,
    client: ComfyClient,
    model_overrides: Mapping[str, str] | None = None,
    lora_overrides: Sequence[Mapping[str, Any]] | None = None,
) -> dict:
    """{"available", "missing_models", "missing_nodes", "error"}.

    Checks the pack's **effective** model set (see effective_required_models)
    against the engine's dropdown enums, and every node class the graph uses
    against the engine's installed node classes. ``model_overrides`` /
    ``lora_overrides`` are this pack's entries from the ``engine_models`` /
    ``engine_loras`` settings.
    """
    missing: list[str] = []
    missing_detail: list[dict] = []
    missing_nodes: list[str] = []
    try:
        required = effective_required_models(pack, model_overrides, lora_overrides)
        for spec, files in required.items():
            class_type, _, input_name = str(spec).partition(".")
            if not class_type or not input_name:
                continue
            enum = set(await client.model_enum(class_type, input_name))
            for f in files or []:
                if f not in enum:
                    missing.append(f)
                    missing_detail.append(
                        {"filename": f, "class_type": class_type, "input": input_name}
                    )
        for class_type in required_node_classes(pack):
            if not await client.has_node_class(class_type):
                missing_nodes.append(class_type)
    except ComfyError as exc:
        return {
            "available": False,
            "missing_models": [],
            "missing_detail": [],
            "missing_nodes": [],
            "error": str(exc),
        }
    return {
        "available": not missing and not missing_nodes,
        "missing_models": missing,
        "missing_detail": missing_detail,
        "missing_nodes": missing_nodes,
        "error": None,
    }


async def pack_model_slots(
    pack: WorkflowPack,
    overrides: dict[str, str],
    client: ComfyClient,
    comfy_models_dir: str = "",
) -> list[dict]:
    """The pack's swappable model slots for the UI.

    Rows: {key, label, node, input, value, baked, options, large_files}.
    ``value`` is the effective file (override or baked); ``options`` is the
    engine's dropdown enum for that loader input ([] when the engine is
    unreachable). When ``comfy_models_dir`` is set (shared filesystem with
    ComfyUI), ``large_files`` lists the options whose on-disk size exceeds
    the big-model guardrail — files that stat as likely too big for a 24 GB
    card; unstattable files are skipped silently.
    """
    slots = pack.manifest.get("model_slots") or []
    if not slots:
        return []
    try:
        graph = pack.load_graph()
    except (OSError, json.JSONDecodeError):
        return []
    rows: list[dict] = []
    for slot in slots:
        key = str(slot.get("key", ""))
        node_id = str(slot.get("node", ""))
        input_name = str(slot.get("input", ""))
        node = graph.get(node_id)
        if not key or node is None or not input_name:
            continue
        baked = str((node.get("inputs") or {}).get(input_name, ""))
        class_type = str(node.get("class_type", ""))
        try:
            options = await client.model_enum(class_type, input_name)
        except ComfyError:
            options = []
        large_files: list[str] = []
        if comfy_models_dir:
            for name in options:
                size = catalog.local_model_size(comfy_models_dir, class_type, name)
                if size is not None and size > catalog.LARGE_FILE_BYTES:
                    large_files.append(name)
        rows.append(
            {
                "key": key,
                "label": str(slot.get("label", key)),
                "node": node_id,
                "input": input_name,
                "value": overrides.get(key) or baked,
                "baked": baked,
                "options": options,
                "large_files": large_files,
            }
        )
    return rows


def pack_lora_stack(pack: WorkflowPack, overrides: list[dict]) -> tuple[list[dict], list[dict]]:
    """(baked stack with overrides applied, added entries) for the UI.

    Baked rows: {node, lora_name, strength, baked_strength, enabled,
    disabled_with_character}. Added rows: {lora_name, strength, enabled}.
    """
    try:
        graph = pack.load_graph()
    except (OSError, json.JSONDecodeError):
        return [], []
    by_node = {str(e["node"]): e for e in overrides if e.get("node")}
    injection = pack.manifest.get("character_injection") or {}
    disable = {str(n) for n in injection.get("disable_nodes") or []}
    stack: list[dict] = []
    for node_id in lora_chain(graph):
        inputs = graph[node_id].get("inputs") or {}
        baked = float(inputs.get("strength_model") or 0)
        override = by_node.get(node_id, {})
        stack.append(
            {
                "node": node_id,
                "lora_name": str(inputs.get("lora_name", "")),
                "strength": float(override.get("strength", baked)),
                "baked_strength": baked,
                "enabled": override.get("enabled", True) is not False,
                "disabled_with_character": node_id in disable,
            }
        )
    added = [
        {
            "lora_name": e["lora_name"],
            "strength": float(e.get("strength", 1.0)),
            "enabled": e.get("enabled", True) is not False,
        }
        for e in overrides
        if e.get("lora_name") and not e.get("node")
    ]
    return stack, added


async def list_workflows(
    settings: Settings,
    comfy_url: str,
    default_ids: dict[str, str] | None = None,
    engine_loras: dict[str, list[dict]] | None = None,
    engine_models: dict[str, dict[str, str]] | None = None,
    comfy_models_dir: str = "",
) -> list[dict]:
    """Registry payload for GET /api/workflows.

    ``default_ids`` maps kind → user-chosen default pack id ("" = unset, fall
    back to the deterministic default). ``engine_loras`` / ``engine_models``
    are the parsed settings of the same names (keyed by pack id).
    ``comfy_models_dir`` (effective setting) enables the big-model warnings on
    model-slot options.
    """
    client = ComfyClient(comfy_url)
    packs = load_packs(settings)
    file_catalog = catalog.load_catalog(settings)
    defaults = {
        kind: (default_ids or {}).get(kind) or default_workflow_id(packs, kind)
        for kind in ("image", "video")
    }
    entries: list[dict] = []
    for pack_id in sorted(packs):
        pack = packs[pack_id]
        manifest = pack.manifest
        overrides = (engine_loras or {}).get(pack.id, [])
        model_overrides = (engine_models or {}).get(pack.id, {})
        availability = await pack_availability(pack, client, model_overrides, overrides)
        stack, added = pack_lora_stack(pack, overrides)
        kind = manifest.get("kind", "image")
        entry = {
            "id": pack.id,
            "name": manifest.get("name", pack.id),
            "kind": kind,
            "description": manifest.get("description", ""),
            "parameters": manifest.get("parameters", []),
            "supports_characters": bool(manifest.get("character_injection")),
            "supports_loras": bool(lora_injection_spec(manifest)),
            "supports_frame_position": bool(manifest.get("frame_conditioning")),
            "available": availability["available"],
            "missing_models": availability["missing_models"],
            "missing_models_info": [
                catalog.model_file_info(d["filename"], d["class_type"], file_catalog)
                for d in availability["missing_detail"]
            ],
            "missing_nodes": availability["missing_nodes"],
            "default": pack.id == defaults.get(kind),
            "loras": stack,
            "added_loras": added,
            "loras_modified": bool(overrides),
            "models": await pack_model_slots(pack, model_overrides, client, comfy_models_dir),
            "models_modified": bool(model_overrides),
        }
        if availability["error"]:
            entry["error"] = availability["error"]
        entries.append(entry)
    return entries
