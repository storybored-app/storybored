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


async def list_workflows(settings: Settings, comfy_url: str) -> list[dict]:
    """Registry payload for GET /api/workflows."""
    client = ComfyClient(comfy_url)
    packs = load_packs(settings)
    entries: list[dict] = []
    for pack_id in sorted(packs):
        pack = packs[pack_id]
        manifest = pack.manifest
        availability = await pack_availability(pack, client)
        entry = {
            "id": pack.id,
            "name": manifest.get("name", pack.id),
            "kind": manifest.get("kind", "image"),
            "description": manifest.get("description", ""),
            "parameters": manifest.get("parameters", []),
            "supports_characters": bool(manifest.get("character_injection")),
            "available": availability["available"],
            "missing_models": availability["missing_models"],
        }
        if availability["error"]:
            entry["error"] = availability["error"]
        entries.append(entry)
    return entries
