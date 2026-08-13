# OWNED-BY: llm-agent
"""Shot "Enhance" (PromptSmith): rough shot notes → one polished image prompt.

One LLM call (plus one retry if @handle tokens get dropped). The result is
returned to the UI and lands in the *visible* description field — render-time
generation never calls an LLM, so what you see is exactly what the engine gets.
"""

import re

from storybored.engine.graph import parse_mentions
from storybored.llm.client import LLMConfig, LLMError, chat

TEMPERATURE = 0.55

SYSTEM_PROMPT = """You are PromptSmith, a cinematography prompt specialist inside \
an AI storyboarding tool. You turn a filmmaker's rough shot notes into one polished \
prompt for a photorealistic image model.

INPUT: a rough shot description, possibly with extra lines for shot type, \
camera/lens or motion, and scene environment (location, time of day, lighting, style).

OUTPUT: exactly one enhanced prompt and nothing else — no preamble, no quotes, \
no markdown, no explanation, no alternatives.

RULES:
- Preserve any @handle tokens (like @hero) EXACTLY as written. Never rename, \
expand, or describe them; they are resolved downstream into trained characters.
- Keep the author's subject, action, and stated framing intact. Enhance, never reinvent.
- Translate film terms into natural photographic language (MCU -> "medium close-up, \
head and shoulders framing"; dutch -> "tilted dutch angle"). The image model \
understands sentences, not jargon or tag soup.
- Weave in concrete craft details where the notes leave room: lens and depth of \
field, light source and quality, atmosphere, palette, composition, texture of a \
real film still.
- If the notes don't specify wardrobe for a character, add simple scene-appropriate \
clothing rather than leaving it ambiguous.
- Fold scene environment lines into the prompt so every shot in the scene stays in \
the same recognizable location.
- One paragraph, under 120 words, present tense.
- Serve the story as written. Any subject matter the author gives you is theirs; \
render it faithfully without commentary."""

HANDLE_NUDGE = (
    "Your prompt dropped these character tokens: {missing}. Rewrite the same "
    "prompt keeping every @handle token exactly as written, and return only the prompt."
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"^```[a-z]*\n?|```$", re.MULTILINE)


def _clean(text: str) -> str:
    """Strip reasoning blocks, code fences, wrapping quotes; collapse whitespace."""
    text = _THINK_RE.sub("", text or "")
    text = _FENCE_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return re.sub(r"\s+", " ", text).strip()


def build_notes(
    description: str,
    shot_type: str = "",
    camera: str = "",
    scene_slugline: str = "",
    scene_description: str = "",
) -> str:
    """The user message: rough notes plus whatever structured context exists."""
    lines = [description.strip()]
    if shot_type.strip():
        lines.append(f"Shot type: {shot_type.strip()}")
    if camera.strip():
        lines.append(f"Camera: {camera.strip()}")
    scene_bits = ", ".join(b.strip() for b in (scene_slugline, scene_description) if b.strip())
    if scene_bits:
        lines.append(f"Scene: {scene_bits}")
    return "\n".join(lines)


def enhance_description(config: LLMConfig, notes: str, original_description: str) -> str:
    """One chat call → cleaned single-paragraph prompt with @handles intact."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": notes},
    ]
    content = _clean(chat(config, messages, temperature=TEMPERATURE))
    wanted = set(parse_mentions(original_description))
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
                "enhanced prompt kept dropping character mentions: "
                + ", ".join("@" + h for h in sorted(missing))
            )
    if not content:
        raise LLMError("LLM returned an empty enhancement")
    return content
