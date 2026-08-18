# OWNED-BY: llm-agent
"""Script → scenes/shots draft breakdown via an OpenAI-compatible LLM.

The system prompt casts the model as a 1st AD and embeds the draft JSON
schema plus the known character handles. Parsing is defensive: strip code
fences, json.loads, and on failure retry once with a "return only valid
JSON" nudge. Nothing here touches the database — the draft is ephemeral.
"""

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from storybored.llm.client import LLMConfig, LLMError, chat
from storybored.llm.guides import guide_block

TEMPERATURE = 0.3

# -- draft schema (mirrors docs/CONTRACT.md) ---------------------------------


class DraftShot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str = ""
    shot_type: str = ""
    camera: str = ""
    dialogue: str = ""
    duration_s: float = 4.0
    characters: list[str] = Field(default_factory=list)


class DraftScene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    slugline: str = ""
    look: str = ""
    shots: list[DraftShot] = Field(default_factory=list)


class BreakdownDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenes: list[DraftScene] = Field(default_factory=list)


DRAFT_SCHEMA = json.dumps(
    {
        "scenes": [
            {
                "title": "short scene title",
                "slugline": "INT./EXT. LOCATION - DAY/NIGHT",
                "look": "the scene's visual environment in one sentence: place, light, weather, palette",
                "shots": [
                    {
                        "description": "what the camera sees, one or two sentences",
                        "shot_type": "WIDE | MED | CU | ECU | OTS | INSERT | ...",
                        "camera": "movement/lens notes, e.g. 'slow push in'",
                        "dialogue": "spoken line(s) covered by this shot, or empty",
                        "duration_s": 4.0,
                        "characters": ["handle"],
                    }
                ],
            }
        ]
    },
    indent=2,
)

SYSTEM_PROMPT = """\
You are a seasoned 1st Assistant Director breaking a script down into a shot list
for a storyboard. Split the script into scenes (use sluglines when present) and
propose a concise, filmable shot list per scene: vary shot sizes, cover dialogue,
keep descriptions visual and concrete, and estimate a duration in seconds for
each shot (typically 2-8). Give every scene a "look": one sentence of concrete
photographic language pinning its visual environment — place, light source and
quality, weather/atmosphere, palette — inferred from the script.

Return ONLY a single JSON object exactly matching this schema (no code fences,
no commentary, no trailing text):

{schema}

Known character handles you may use in each shot's "characters" array (use the
bare handle without @):
{handles}

Tag a known character ONLY when that specific character clearly appears in that
shot (named or unmistakably present in its action/dialogue). If none of the known
characters clearly appear in a shot, return "characters": [] for that shot — do
NOT tag a character just because it is the only known one. Character tagging is
best-effort: when in doubt, leave the shot untagged. Prefer coverage where each
shot features ONE character (singles, shot/reverse-shot) — trained-character
identity applies to every face in a frame, so shots tagged with two characters
render them as look-alikes.

If a character in the script is not in the known list, leave them out of
"characters" but still describe them in the shot description.
"""

VIBES_SYSTEM_PROMPT = """\
You are a director and cinematographer team turning a freeform story — rough
prose, an idea, a vibe — into a filmable storyboard. Invent the cinematic
coverage yourself: split the story into scenes with proper sluglines
(INT./EXT. LOCATION - TIME), then design a varied, intentional shot list per
scene (establishing wides, coverage, inserts, a closer beat for emotion), with
a duration in seconds for each shot (typically 2-8).

Every shot "description" must be a finished, render-ready image prompt for a
photorealistic still — not a note. In each description:
- Write one present-tense paragraph of concrete photographic language: framing,
  lens and depth of field, light source and quality, atmosphere, palette,
  texture of a real film still.
- Repeat the scene's location, time of day, and lighting in EVERY shot of that
  scene so all its shots read as the same recognizable place — describe the
  place in natural words ("a lonely desert gas station at dawn"), never by
  pasting the slugline text into the description.
- Also fill the scene's "look" field: one sentence of concrete photographic
  language pinning that environment (place, light source and quality, weather,
  palette) — the same environment your shot descriptions repeat.
- When a known character (listed below) appears, reference them inline with
  their @handle (e.g. "@ava turns from the window") — never a plain name. Keep
  the @handle token exactly as given.
- ONE @handle per shot: trained-character identity applies to every visible
  face in a frame, so two tagged characters blend into look-alikes. Give each
  shot a single featured character, and stage anyone else facelessly — seen
  from behind, silhouetted, distant, cropped, or out of focus. Cover two-person
  beats as alternating singles (shot/reverse-shot), never as two clear faces
  in one frame.
- Always state what characters are wearing; pick simple scene-appropriate
  wardrobe if the story doesn't say, and keep it consistent across the scene.
- Put camera movement in the "camera" field, not the description.

Return ONLY a single JSON object exactly matching this schema (no code fences,
no commentary, no trailing text):

{schema}

Known character handles (bare handle without @) for the "characters" arrays:
{handles}

Tag a known character ONLY when that specific character clearly appears in that
shot. Story characters not in the known list: describe them in the description
(with wardrobe), but leave them out of "characters".
"""

RETRY_NUDGE = (
    "That was not valid JSON. Return only valid JSON matching the schema — "
    "no code fences, no commentary, nothing before or after the JSON object."
)

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


#: lead-in that frames the engine guide for shot descriptions (both modes:
#: vibes descriptions ARE render prompts; script descriptions still seed them)
GUIDE_LEAD_IN = (
    "The shot descriptions you write will be rendered as stills by the image "
    "engine below — lean toward its preferred prompt style when describing shots:"
)


def build_system_prompt(
    known_handles: list[str], mode: str = "script", guide: dict | None = None
) -> str:
    handles = ", ".join(sorted(h.lstrip("@") for h in known_handles)) or "(none yet)"
    template = VIBES_SYSTEM_PROMPT if mode == "vibes" else SYSTEM_PROMPT
    prompt = template.format(schema=DRAFT_SCHEMA, handles=handles)
    block = guide_block(guide)
    if block:
        prompt += "\n" + GUIDE_LEAD_IN + "\n" + block
    return prompt


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    return text


def parse_draft(text: str) -> BreakdownDraft:
    """Best-effort parse: as-is → fence-stripped → outermost {...} slice."""
    candidates = [text.strip(), _strip_fences(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    last_error: Exception = LLMError("empty LLM response")
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return BreakdownDraft.model_validate(json.loads(candidate))
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
    raise last_error


def breakdown_script(
    config: LLMConfig,
    script_text: str,
    known_handles: list[str],
    mode: str = "script",
    guide: dict | None = None,
) -> BreakdownDraft:
    """One LLM call (plus one retry on unparseable output) → validated draft.

    mode "script" = 1st-AD breakdown of a formatted screenplay; mode "vibes" =
    freeform story → invented coverage with render-ready shot descriptions.
    ``guide`` is the default image engine's prompt guide (llm/guides.py).
    """
    messages = [
        {"role": "system", "content": build_system_prompt(known_handles, mode, guide)},
        {"role": "user", "content": script_text},
    ]
    content = chat(config, messages, temperature=TEMPERATURE)
    try:
        return parse_draft(content)
    except (json.JSONDecodeError, ValidationError):
        retry_messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": RETRY_NUDGE},
        ]
        content = chat(config, retry_messages, temperature=TEMPERATURE)
        try:
            return parse_draft(content)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise LLMError(
                "The model did not return a valid breakdown draft (invalid JSON after retry)."
            ) from exc
