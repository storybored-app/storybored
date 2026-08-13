# OWNED-BY: engine-agent
"""Workflow-pack registry.

Packs are folders containing ``manifest.json`` + a ComfyUI API-format graph.
Two locations are scanned: the repo's ``workflows/`` dir and
``DATA_DIR/workflows`` (user-installed packs; same id overrides the repo one).

Availability: every file listed in the manifest's ``required_models``
(``"ClassType.input_name": [filenames]``) is checked against the ComfyUI
/object_info dropdown enums (cached 60s by the client). Packs with missing
models are flagged, never hidden.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from storybored.config import Settings
from storybored.engine.comfy_client import ComfyClient, ComfyError
from storybored.engine.graph import lora_chain, lora_injection_spec

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


async def pack_availability(pack: WorkflowPack, client: ComfyClient) -> dict:
    """{"available": bool, "missing_models": [...], "error": str | None}."""
    missing: list[str] = []
    try:
        for spec, files in (pack.manifest.get("required_models") or {}).items():
            class_type, _, input_name = str(spec).partition(".")
            if not class_type or not input_name:
                continue
            enum = set(await client.model_enum(class_type, input_name))
            missing.extend(f for f in files or [] if f not in enum)
    except ComfyError as exc:
        return {"available": False, "missing_models": [], "error": str(exc)}
    return {"available": not missing, "missing_models": missing, "error": None}


async def pack_model_slots(
    pack: WorkflowPack, overrides: dict[str, str], client: ComfyClient
) -> list[dict]:
    """The pack's swappable model slots for the UI.

    Rows: {key, label, node, input, value, baked, options}. ``value`` is the
    effective file (override or baked); ``options`` is the engine's dropdown
    enum for that loader input ([] when the engine is unreachable).
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
        try:
            options = await client.model_enum(str(node.get("class_type", "")), input_name)
        except ComfyError:
            options = []
        rows.append(
            {
                "key": key,
                "label": str(slot.get("label", key)),
                "node": node_id,
                "input": input_name,
                "value": overrides.get(key) or baked,
                "baked": baked,
                "options": options,
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
) -> list[dict]:
    """Registry payload for GET /api/workflows.

    ``default_ids`` maps kind → user-chosen default pack id ("" = unset, fall
    back to the deterministic default). ``engine_loras`` / ``engine_models``
    are the parsed settings of the same names (keyed by pack id).
    """
    client = ComfyClient(comfy_url)
    packs = load_packs(settings)
    defaults = {
        kind: (default_ids or {}).get(kind) or default_workflow_id(packs, kind)
        for kind in ("image", "video")
    }
    entries: list[dict] = []
    for pack_id in sorted(packs):
        pack = packs[pack_id]
        manifest = pack.manifest
        availability = await pack_availability(pack, client)
        overrides = (engine_loras or {}).get(pack.id, [])
        model_overrides = (engine_models or {}).get(pack.id, {})
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
            "default": pack.id == defaults.get(kind),
            "loras": stack,
            "added_loras": added,
            "loras_modified": bool(overrides),
            "models": await pack_model_slots(pack, model_overrides, client),
            "models_modified": bool(model_overrides),
        }
        if availability["error"]:
            entry["error"] = availability["error"]
        entries.append(entry)
    return entries
