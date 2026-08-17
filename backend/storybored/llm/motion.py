# OWNED-BY: llm-agent
"""Motion "Generate" (PromptSmith): shot details → one MiniMax i2v motion prompt.

Same product rule as the image Enhance pass: one LLM call (plus one retry if
@handle tokens from the author's own motion notes get dropped), and the result
lands in the *visible* Motion field — render-time generation never calls an LLM.

The prompt is frame-position aware: the shot's still either opens the clip
(motion flows forward from it) or closes it (motion arrives at it).
"""

from storybored.engine.graph import parse_mentions
from storybored.llm.client import LLMConfig, LLMError, chat
from storybored.llm.enhance import HANDLE_NUDGE, _clean
from storybored.llm.guides import guide_block

TEMPERATURE = 0.55

SYSTEM_PROMPT = """You are PromptSmith, a cinematography prompt specialist inside \
an AI storyboarding tool. You write the MOTION prompt for an image-to-video model: \
a single still from the shot is provided to the model, and your prompt describes \
how the clip moves and sounds.

INPUT: the shot's image prompt (what the still shows), possibly with extra lines \
for shot type, camera move, rough motion notes, dialogue, clip length, scene \
context, and whether the still is the FIRST or the LAST frame of the clip.

OUTPUT: exactly one motion prompt and nothing else — no preamble, no quotes, \
no markdown, no explanation, no alternatives.

RULES:
- Describe motion, not appearance: how the subject moves, how the camera moves, \
how the environment lives (wind, rain, steam, flicker). The model already sees \
the still — never re-describe what it looks like.
- Preserve any @handle tokens (like @hero) EXACTLY as written when referring to \
those people. Never rename, expand, or describe them.
- If the still is the FIRST frame, motion flows forward out of that moment. If \
it is the LAST frame, describe action that builds toward and settles into that \
exact composition — the clip must end on it.
- Honor the author's rough motion notes and stated camera move; enhance, never \
reinvent. With no notes, invent modest, physically plausible motion that suits \
the shot — subtle beats grand.
- End with one sentence starting "Audio:" describing the soundscape. If dialogue \
is given, put the spoken line there (e.g. Audio: the woman says "…" over soft \
room tone).
- One paragraph, under 90 words, present tense, plain sentences — no jargon or \
tag soup.
- Serve the story as written. Any subject matter the author gives you is theirs; \
render it faithfully without commentary."""


def build_motion_notes(
    description: str,
    shot_type: str = "",
    camera: str = "",
    motion_prompt: str = "",
    dialogue: str = "",
    duration_s: float = 0.0,
    scene_slugline: str = "",
    scene_description: str = "",
    frame_position: str = "first",
) -> str:
    """The user message: everything we know about the shot, one line each."""
    lines = [f"Image prompt (what the still shows): {description.strip()}"]
    if shot_type.strip():
        lines.append(f"Shot type: {shot_type.strip()}")
    if camera.strip():
        lines.append(f"Camera: {camera.strip()}")
    if motion_prompt.strip():
        lines.append(f"Rough motion notes: {motion_prompt.strip()}")
    if dialogue.strip():
        lines.append(f"Dialogue: {dialogue.strip()}")
    if duration_s > 0:
        lines.append(f"Clip length: about {duration_s:g} seconds")
    scene_bits = ", ".join(b.strip() for b in (scene_slugline, scene_description) if b.strip())
    if scene_bits:
        lines.append(f"Scene: {scene_bits}")
    lines.append(
        "The still is the LAST frame of the clip — motion must arrive at it."
        if frame_position == "last"
        else "The still is the FIRST frame of the clip — motion flows forward from it."
    )
    return "\n".join(lines)


def system_prompt(guide: dict | None = None) -> str:
    """The motion system prompt, plus the active video engine's prompt guide
    (see llm/guides.py) when the render engine declares one."""
    block = guide_block(guide)
    return SYSTEM_PROMPT + ("\n\n" + block if block else "")


def generate_motion_prompt(
    config: LLMConfig, notes: str, rough_motion: str, guide: dict | None = None
) -> str:
    """One chat call → cleaned single-paragraph motion prompt.

    @handles from the author's own rough motion notes must survive (one nudge
    retry, then error); handles only in the image prompt are optional — a
    motion prompt legitimately may not name every character.
    """
    messages = [
        {"role": "system", "content": system_prompt(guide)},
        {"role": "user", "content": notes},
    ]
    content = _clean(chat(config, messages, temperature=TEMPERATURE))
    wanted = set(parse_mentions(rough_motion))
    missing = wanted - set(parse_mentions(content))
    if missing:
        retry = messages + [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": HANDLE_NUDGE.format(
                    missing=", ".join("@" + h for h in sorted(missing))
                ),
            },
        ]
        content = _clean(chat(config, retry, temperature=TEMPERATURE))
        missing = wanted - set(parse_mentions(content))
        if missing:
            raise LLMError(
                "generated motion prompt kept dropping character mentions: "
                + ", ".join("@" + h for h in sorted(missing))
            )
    if not content:
        raise LLMError("LLM returned an empty motion prompt")
    return content
