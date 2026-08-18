# OWNED-BY: engine-agent
"""`python -m storybored validate-pack <dir>` — offline workflow-pack linter.

Checks a pack folder (manifest.json + API-format graph) without touching any
ComfyUI: the manifest parses and has a sane shape, every node id it references
exists in the graph, parameter types are known, and the manifest's
``required_models`` matches what the graph's loader nodes actually load
(``--write`` updates the manifest from the graph, which beats transcribing
filenames by hand).

Exit codes (CI-friendly):
  0  every pack valid (warnings allowed)
  1  at least one validation error
  2  usage / IO problem (folder missing, unreadable files)
"""

import argparse
import json
import re
from pathlib import Path

from storybored.engine.registry import MAX_PROMPT_GUIDE_EXAMPLES

#: manifest parameter types the engine understands (docs/WORKFLOWS.md)
KNOWN_PARAM_TYPES = {"prompt", "seed", "int", "float", "string", "image"}

#: core ComfyUI loader classes → the input that names a model file. This is
#: what `required_models` derivation walks. Custom loader classes aren't
#: derivable offline — list their files in the manifest by hand (a WARN flags
#: likely candidates).
LOADER_INPUTS: dict[str, tuple[str, ...]] = {
    "UNETLoader": ("unet_name",),
    "CLIPLoader": ("clip_name",),
    "DualCLIPLoader": ("clip_name1", "clip_name2"),
    "VAELoader": ("vae_name",),
    "LoraLoader": ("lora_name",),
    "LoraLoaderModelOnly": ("lora_name",),
    "CheckpointLoaderSimple": ("ckpt_name",),
}

#: file suffixes that make an underivable string input smell like a model file
_MODEL_SUFFIXES = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".gguf", ".bin")


class PackReport:
    """Collected findings for one pack folder."""

    def __init__(self, pack_dir: Path) -> None:
        self.pack_dir = pack_dir
        self.errors: list[str] = []
        self.warnings: list[str] = []
        #: derived vs manifest required_models (set by validate_pack)
        self.derived_required: dict[str, list[str]] = {}
        self.drift_missing: dict[str, list[str]] = {}  # graph loads, manifest omits
        self.drift_extra: dict[str, list[str]] = {}  # manifest lists, graph doesn't load

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def has_drift(self) -> bool:
        return bool(self.drift_missing or self.drift_extra)


def derive_required_models(graph: dict) -> dict[str, list[str]]:
    """``required_models`` as the graph's loader nodes imply it.

    Walks every node whose class is a known loader and collects the string
    value of its model-name input (links and non-strings are skipped).
    """
    derived: dict[str, list[str]] = {}
    for node_id in sorted(graph, key=str):
        node = graph[node_id]
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        for input_name in LOADER_INPUTS.get(str(node.get("class_type", "")), ()):
            value = inputs.get(input_name)
            if isinstance(value, str) and value:
                spec = f"{node['class_type']}.{input_name}"
                files = derived.setdefault(spec, [])
                if value not in files:
                    files.append(value)
    return derived


def _check_node_ref(report: PackReport, graph: dict, node_id: object, what: str) -> None:
    if str(node_id) not in graph:
        report.error(f"{what} points at node '{node_id}' which is not in the graph")


def _check_parameters(report: PackReport, manifest: dict, graph: dict) -> None:
    params = manifest.get("parameters")
    if not isinstance(params, list):
        report.error("manifest 'parameters' must be a list")
        return
    seen_keys: set[str] = set()
    prompt_count = 0
    for i, spec in enumerate(params):
        if not isinstance(spec, dict):
            report.error(f"parameters[{i}] must be an object")
            continue
        key = spec.get("key")
        if not key:
            report.error(f"parameters[{i}] has no 'key'")
        elif key in seen_keys:
            report.error(f"duplicate parameter key '{key}'")
        else:
            seen_keys.add(key)
        ptype = spec.get("type", "")
        if ptype not in KNOWN_PARAM_TYPES:
            report.error(
                f"parameter '{key}' has unknown type '{ptype}' "
                f"(known: {', '.join(sorted(KNOWN_PARAM_TYPES))})"
            )
        if ptype == "prompt":
            prompt_count += 1
        if not spec.get("node") or not spec.get("input"):
            report.error(f"parameter '{key}' needs both 'node' and 'input'")
        else:
            _check_node_ref(report, graph, spec["node"], f"parameter '{key}'")
    if manifest.get("kind") == "image" and prompt_count != 1:
        report.warn(
            f"image pack has {prompt_count} 'prompt' parameters — the shot "
            "description needs exactly one to land in the graph"
        )


def _check_injections(report: PackReport, manifest: dict, graph: dict) -> None:
    ci = manifest.get("character_injection")
    if ci is not None:
        if not isinstance(ci, dict) or not ci.get("after_node"):
            report.error("character_injection must be an object with 'after_node'")
        else:
            _check_node_ref(report, graph, ci["after_node"], "character_injection.after_node")
            for node_id in ci.get("disable_nodes") or []:
                _check_node_ref(report, graph, node_id, "character_injection.disable_nodes")
            _check_injection_class(report, ci, "character_injection")
    li = manifest.get("lora_injection")
    if li is not None:
        if not isinstance(li, dict) or not li.get("after_node"):
            report.error("lora_injection must be an object with 'after_node'")
        else:
            _check_node_ref(report, graph, li["after_node"], "lora_injection.after_node")
            _check_injection_class(report, li, "lora_injection")
    fc = manifest.get("frame_conditioning")
    if fc is not None:
        if not isinstance(fc, dict) or not fc.get("node"):
            report.error("frame_conditioning must be an object with 'node'")
        else:
            _check_node_ref(report, graph, fc["node"], "frame_conditioning.node")
            node = graph.get(str(fc["node"]))
            first = str(fc.get("first", "first_frame"))
            if node is not None and first not in (node.get("inputs") or {}):
                report.error(
                    f"frame_conditioning node '{fc['node']}' has no '{first}' input "
                    "to move onto the last-frame input"
                )


#: LoRA loader classes an injection seam may splice (engine/graph.py)
_INJECTION_CLASSES = ("LoraLoader", "LoraLoaderModelOnly")


def _check_injection_class(report: PackReport, spec: dict, what: str) -> None:
    class_type = spec.get("class_type")
    if class_type is not None and class_type not in _INJECTION_CLASSES:
        report.error(
            f"{what}.class_type must be one of {', '.join(_INJECTION_CLASSES)} "
            f"(got {class_type!r}) — the engine can only splice LoRA loaders"
        )


def _check_license_note(report: PackReport, manifest: dict) -> None:
    """license_note is a UI string — warn on a non-string so it isn't silently
    dropped by the registry (which str()s whatever it finds)."""
    note = manifest.get("license_note")
    if note is not None and (not isinstance(note, str) or not note.strip()):
        report.warn("license_note should be a non-empty string — it will be ignored")


def _check_prompt_guide(report: PackReport, manifest: dict) -> None:
    """Mirror registry.sanitize_prompt_guide as WARNs — a malformed guide is
    silently dropped at load time, so the linter is where authors learn why."""
    guide = manifest.get("prompt_guide")
    if guide is None:
        return
    style = guide.get("style") if isinstance(guide, dict) else None
    if not isinstance(style, str) or not style.strip():
        report.warn(
            "prompt_guide will be ignored — it must be an object with a non-empty "
            'string "style" (and optional "examples" list)'
        )
        return
    examples = guide.get("examples", [])
    if not isinstance(examples, list):
        report.warn('prompt_guide "examples" must be a list of strings — it will be ignored')
        return
    if any(not isinstance(e, str) or not e.strip() for e in examples):
        report.warn("prompt_guide has non-string/empty examples — they will be dropped")
    if len(examples) > MAX_PROMPT_GUIDE_EXAMPLES:
        report.warn(
            f"prompt_guide has {len(examples)} examples — only the first "
            f"{MAX_PROMPT_GUIDE_EXAMPLES} are used"
        )


#: lora_family ids are lowercase slugs ("krea2", "z-image", "qwen-image") —
#: character-LoRA compatibility matches the id string exactly
_FAMILY_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _check_lora_family(report: PackReport, manifest: dict) -> None:
    """Optional ``lora_family``: the model family this pack's character LoRAs
    must belong to. Absent = family-agnostic (no compatibility checks)."""
    family = manifest.get("lora_family")
    if family is None:
        return
    if not isinstance(family, str) or not family.strip():
        report.error(
            'lora_family must be a non-empty string (e.g. "krea2", "z-image", '
            '"qwen-image") or omitted for a family-agnostic pack'
        )
        return
    if not _FAMILY_SLUG_RE.match(family):
        report.warn(
            f"lora_family '{family}' is not a lowercase slug — character-LoRA "
            "compatibility matches this id exactly, so keep it stable and slug-shaped"
        )


def _check_model_slots(report: PackReport, manifest: dict, graph: dict) -> None:
    for i, slot in enumerate(manifest.get("model_slots") or []):
        if not isinstance(slot, dict):
            report.error(f"model_slots[{i}] must be an object")
            continue
        label = slot.get("key") or f"model_slots[{i}]"
        if not slot.get("key") or not slot.get("node") or not slot.get("input"):
            report.error(f"model slot '{label}' needs 'key', 'node' and 'input'")
            continue
        _check_node_ref(report, graph, slot["node"], f"model slot '{label}'")


def _check_required_models_drift(report: PackReport, manifest: dict, graph: dict) -> None:
    derived = derive_required_models(graph)
    report.derived_required = derived
    declared = {
        str(spec): [str(f) for f in files or []]
        for spec, files in (manifest.get("required_models") or {}).items()
    }
    for spec, files in derived.items():
        missing = [f for f in files if f not in declared.get(spec, [])]
        if missing:
            report.drift_missing[spec] = missing
    for spec, files in declared.items():
        extra = [f for f in files if f not in derived.get(spec, [])]
        if extra:
            report.drift_extra[spec] = extra
    # nodes that look like they load models but aren't in the loader table
    for node_id, node in graph.items():
        if not isinstance(node, dict) or node.get("class_type") in LOADER_INPUTS:
            continue
        for input_name, value in (node.get("inputs") or {}).items():
            if isinstance(value, str) and value.lower().endswith(_MODEL_SUFFIXES):
                report.warn(
                    f"node '{node_id}' ({node.get('class_type')}) input "
                    f"'{input_name}' looks like a model file ('{value}') but the "
                    "class isn't a known loader — if it loads a file, add it to "
                    "required_models by hand"
                )


def validate_pack(pack_dir: Path) -> PackReport:
    """Run every offline check against one pack folder."""
    report = PackReport(pack_dir)
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        report.error("no manifest.json in this folder")
        return report
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.error(f"manifest.json does not parse: {exc}")
        return report
    if not isinstance(manifest, dict):
        report.error("manifest.json must be a JSON object")
        return report

    pack_id = manifest.get("id")
    if not pack_id or not isinstance(pack_id, str):
        report.error("manifest needs a string 'id'")
    elif pack_id != pack_dir.name:
        report.error(f"manifest id '{pack_id}' does not match folder name '{pack_dir.name}'")
    if not manifest.get("name"):
        report.warn("manifest has no 'name' — the engine selector will show the id")
    kind = manifest.get("kind")
    if kind not in ("image", "video"):
        report.error(f"manifest 'kind' must be \"image\" or \"video\" (got {kind!r})")

    graph_path = pack_dir / (manifest.get("graph") or "graph.json")
    if not graph_path.is_file():
        report.error(f"graph file '{graph_path.name}' not found")
        return report
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.error(f"{graph_path.name} does not parse: {exc}")
        return report
    if not isinstance(graph, dict) or not all(
        isinstance(n, dict) and "class_type" in n for n in graph.values()
    ):
        report.error(
            f"{graph_path.name} is not an API-format graph (top-level keys must be "
            "node ids mapping to objects with a class_type — use ComfyUI's "
            "\"Save (API Format)\", not plain Save)"
        )
        return report

    output_node = manifest.get("output_node")
    if not output_node:
        report.error("manifest needs an 'output_node' (the SaveImage/SaveVideo node id)")
    else:
        _check_node_ref(report, graph, output_node, "output_node")

    _check_parameters(report, manifest, graph)
    _check_injections(report, manifest, graph)
    _check_model_slots(report, manifest, graph)
    _check_prompt_guide(report, manifest)
    _check_license_note(report, manifest)
    _check_lora_family(report, manifest)
    for cls in manifest.get("required_nodes") or []:
        if not isinstance(cls, str) or not cls:
            report.error("required_nodes entries must be non-empty class-name strings")
    _check_required_models_drift(report, manifest, graph)
    return report


def write_required_models(pack_dir: Path) -> None:
    """Replace the manifest's required_models with the graph-derived set."""
    manifest_path = pack_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    graph_path = pack_dir / (manifest.get("graph") or "graph.json")
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    manifest["required_models"] = derive_required_models(graph)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _print_report(report: PackReport) -> None:
    rel = report.pack_dir
    for msg in report.errors:
        print(f"{rel}: ERROR: {msg}")
    for msg in report.warnings:
        print(f"{rel}: WARN: {msg}")
    for spec, files in report.drift_missing.items():
        for f in files:
            print(f"{rel}: DRIFT: graph loads {spec} '{f}' but required_models omits it")
    for spec, files in report.drift_extra.items():
        for f in files:
            print(
                f"{rel}: DRIFT: required_models lists {spec} '{f}' "
                "but no loader in the graph loads it"
            )
    if report.ok and not report.has_drift:
        print(f"{rel}: OK")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="storybored validate-pack",
        description="Validate workflow pack folder(s) offline (no ComfyUI needed).",
    )
    parser.add_argument("dirs", nargs="+", help="pack folder(s), each holding manifest.json")
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite each manifest's required_models from the graph's loader nodes",
    )
    args = parser.parse_args(argv)

    failed = False
    for raw in args.dirs:
        pack_dir = Path(raw)
        if not pack_dir.is_dir():
            print(f"{pack_dir}: ERROR: not a directory")
            return 2
        report = validate_pack(pack_dir)
        if args.write and report.ok and report.has_drift:
            write_required_models(pack_dir)
            print(f"{pack_dir}: wrote required_models from the graph")
            report = validate_pack(pack_dir)
        _print_report(report)
        # drift is a real error: it is exactly the stale-manifest bug this
        # command exists to catch (run with --write to fix it)
        if not report.ok or report.has_drift:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
