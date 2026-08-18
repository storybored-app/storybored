# OWNED-BY: llm-agent
"""Portrait "Generate" (PromptSmith): a character's bio → one portrait prompt.

Same product rule as the Enhance and Motion passes: one LLM call (plus one
retry if the character's @handle gets dropped). The generate-thumbnail
endpoint runs this BEFORE enqueueing the render job and returns the drafted
prompt in its response — the render job itself never calls an LLM, and a
character without a bio keeps the stock studio portrait.
"""

from storybored.engine.graph import parse_mentions
from storybored.llm.client import LLMConfig, LLMError, chat
from storybored.llm.enhance import HANDLE_NUDGE, _clean
from storybored.llm.guides import guide_block

TEMPERATURE = 0.7

SYSTEM_PROMPT = """You are PromptSmith, a portrait specialist inside an AI \
storyboarding tool. You write the prompt for one cast member's card photo — \
the headshot that introduces them as an individual.

INPUT: the character's @handle, their name, and a short bio (personality, \
vibe, backstory, style notes).

OUTPUT: exactly one image prompt and nothing else — no preamble, no quotes, \
no markdown, no explanation, no alternatives.

RULES:
- Start with "portrait photograph of @handle", keeping the given @handle \
token EXACTLY as written. Never rename, expand, or describe the token itself.
- Express who they are through what a camera can see: expression, gaze, \
posture, grooming, wardrobe, lighting mood, and a simple backdrop that suits \
them. Translate bio facts into visible cues — never recite biography as text.
- ALWAYS name one concrete, fully-covering outfit (specific garment plus \
color or fabric, collar or neckline visible) chosen to fit the bio. Never \
leave clothing unstated.
- Head-and-shoulders or chest-up framing, a single subject, face unobstructed \
and in sharp focus, photorealistic.
- Keep the backdrop simple — studio seamless or one softly blurred \
environment — so the face carries the image.
- One paragraph, under 60 words, plain sentences — no tag soup.
- Serve the bio as written. Any subject matter the author gives you is \
theirs; render it faithfully without commentary."""


def build_portrait_notes(name: str, handle: str, class_word: str, bio: str) -> str:
    """The user message: who the portrait is of, one line each."""
    lines = [f"Character: {name.strip()} (@{handle}), a {class_word.strip() or 'person'}"]
    if bio.strip():
        lines.append(f"Bio: {bio.strip()}")
    return "\n".join(lines)


def system_prompt(guide: dict | None = None) -> str:
    """The portrait system prompt, plus the image engine's prompt guide
    (see llm/guides.py) when the render engine declares one."""
    block = guide_block(guide)
    return SYSTEM_PROMPT + ("\n\n" + block if block else "")


def generate_portrait_prompt(
    config: LLMConfig, notes: str, handle: str, guide: dict | None = None
) -> str:
    """One chat call → cleaned single-paragraph portrait prompt.

    The character's own @handle must survive — it is what splices their LoRA
    trigger into the render (one nudge retry, then error).
    """
    messages = [
        {"role": "system", "content": system_prompt(guide)},
        {"role": "user", "content": notes},
    ]
    content = _clean(chat(config, messages, temperature=TEMPERATURE))
    if handle not in parse_mentions(content):
        retry = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": HANDLE_NUDGE.format(missing="@" + handle)},
        ]
        content = _clean(chat(config, retry, temperature=TEMPERATURE))
        if handle not in parse_mentions(content):
            raise LLMError(
                f"generated portrait prompt kept dropping the @{handle} mention"
            )
    if not content:
        raise LLMError("LLM returned an empty portrait prompt")
    return content
