# OWNED-BY: training-agent
"""Model-family identity for character LoRAs.

A character LoRA is bound to the model family it was trained on: one trained
for Krea 2 is inert on Z-Image or Qwen-Image (different transformer, different
weight shapes). Image packs declare their family via the optional
``lora_family`` manifest key; characters record theirs in the nullable
``character.lora_family`` column. An absent value on either side means
"unknown / family-agnostic" and never blocks anything — only two *known,
different* families conflict.

Family ids are open vocabulary (a user pack may declare any slug); the ids the
shipped packs and the lora-factory trainer use are listed in
``FAMILY_LABELS`` so the UI can show a human name.
"""

#: family id → human label for the shipped families (open vocabulary beyond)
FAMILY_LABELS: dict[str, str] = {
    "krea2": "Krea 2",
    "z-image": "Z-Image",
    "qwen-image": "Qwen-Image",
}

#: historical trainer default: before families existed, every lora-factory
#: run produced a Krea 2 LoRA — a family-agnostic engine keeps that behavior
DEFAULT_TRAINING_FAMILY = "krea2"


def family_label(family_id: str) -> str:
    """Human name for a family id ("z-image" → "Z-Image"); unknown ids pass
    through unchanged so user-defined families still read sensibly."""
    return FAMILY_LABELS.get(family_id, family_id)


def pack_family(manifest: dict) -> str:
    """The pack's declared LoRA family ("" = family-agnostic)."""
    return str(manifest.get("lora_family") or "")
