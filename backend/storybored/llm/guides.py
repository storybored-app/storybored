# OWNED-BY: llm-agent
"""Per-engine prompt guides: teach PromptSmith how the ACTIVE engine
wants to be prompted.

A workflow-pack manifest may carry an optional ``prompt_guide`` key
(``{"style": "<one-paragraph description>", "examples": [...]}`` — validated
leniently at pack load, see engine/registry.sanitize_prompt_guide). Every
LLM prompt-assembly path (Enhance, breakdown/story-vibes, generate-motion)
injects the guide of the engine that will actually render the result, so
generated prompts land in that model's preferred dialect instead of a
generic one.
"""

from sqlmodel import Session

from storybored.config import Settings
from storybored.engine import registry
from storybored.settings_store import effective_setting


def resolve_prompt_guide(
    session: Session,
    settings: Settings,
    kind: str = "image",
    workflow_id: str | None = None,
) -> dict | None:
    """The ``prompt_guide`` of the engine that will actually render.

    Resolution mirrors the generate endpoints (api/preflight.resolve_pack):
    the explicitly selected pack, else the configured ``default_{kind}_workflow``
    setting, else the deterministic first pack of the kind — but lenient:
    an unknown pack, a kind mismatch, or a guideless pack yields ``None``
    and the caller's prompt simply carries no engine section.

    Returns ``{"engine": <display name>, "style": str, "examples": [str]}``.
    """
    packs = registry.load_packs(settings)
    resolved = (
        workflow_id
        or effective_setting(session, settings, f"default_{kind}_workflow")
        or registry.default_workflow_id(packs, kind=kind)
    )
    pack = packs.get(resolved or "")
    if pack is None or pack.manifest.get("kind", "image") != kind:
        return None
    guide = pack.manifest.get("prompt_guide")
    if not guide:
        return None
    return {
        "engine": str(pack.manifest.get("name") or pack.id),
        "style": guide["style"],
        "examples": list(guide.get("examples") or []),
    }


def guide_block(guide: dict | None) -> str:
    """The compact, clearly-delimited system-prompt section for a guide.

    Empty string when there is no guide — callers append it verbatim.
    """
    if not guide:
        return ""
    lines = [
        f"=== ACTIVE RENDER ENGINE: {guide['engine']} ===",
        "The prompt will be rendered by this engine. How it wants to be prompted:",
        guide["style"],
    ]
    if guide["examples"]:
        lines.append("Example prompts in this engine's preferred style:")
        lines.extend(f"{i}. {ex}" for i, ex in enumerate(guide["examples"], 1))
    lines.append("=== END ACTIVE RENDER ENGINE ===")
    return "\n".join(lines)
