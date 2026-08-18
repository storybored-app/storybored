"""Application settings, loaded from environment / .env (pydantic-settings)."""

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    """Stable base for resolving .env / DATA_DIR, independent of the CWD.

    Defaults to the repo/package root (three levels up from this file:
    backend/storybored/config.py -> repo root). Override with STORYBORED_HOME
    to relocate config + data (e.g. a packaged install)."""
    override = os.environ.get("STORYBORED_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


#: Anchored once at import so the app finds the same .env / data dir no matter
#: which directory the user launched it from.
PROJECT_ROOT = _project_root()


class Settings(BaseSettings):
    """All machine config lives here; every field maps to an UPPER_CASE env var."""

    model_config = SettingsConfigDict(
        # Absolute path so `.env` is read from the project root, not the CWD —
        # otherwise launching from another directory silently loses config.
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    storybored_host: str = Field(
        default="127.0.0.1",
        description=(
            "Network interface to bind. Defaults to 127.0.0.1 (this machine "
            "only). Set to 0.0.0.0 to expose on your LAN — the app has NO "
            "authentication, so only do that on a network you trust."
        ),
    )
    storybored_port: int = 8600
    data_dir: str = Field(
        default="./data",
        description=(
            "Where projects, media and the database are stored. A relative "
            "path is resolved against the project root (not the current "
            "directory), so the same data is used from any launch location."
        ),
    )
    comfyui_url: str = "http://127.0.0.1:8188"
    comfy_loras_dir: str = ""
    comfy_models_dir: str = Field(
        default="",
        description=(
            "Optional: ComfyUI's base models directory (the folder containing "
            "diffusion_models/, text_encoders/, vae/, loras/). Only meaningful "
            "when StoryBored and ComfyUI share a filesystem — enables the "
            "in-app model downloader and the big-model size warnings."
        ),
    )
    comfy_mode_image_cmd: str = ""
    comfy_mode_video_cmd: str = ""
    comfy_flush_cmd: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_keep_alive: str = Field(
        default="",
        description=(
            "Optional: passed through as the keep_alive field on chat calls "
            "when set. Ollama-specific: '0' unloads the model from VRAM "
            "immediately after each call (good on a GPU shared with the "
            "render engine), '10m' keeps it warm. Leave empty for the "
            "provider default, and leave empty for non-Ollama providers — "
            "strict OpenAI-compatible endpoints may reject unknown fields."
        ),
    )
    lora_factory_dir: str = ""

    @property
    def data_path(self) -> Path:
        # Relative DATA_DIR is anchored to the project root so it resolves the
        # same regardless of the process working directory.
        path = Path(self.data_dir).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def media_path(self) -> Path:
        return self.data_path / "media"

    @property
    def exports_path(self) -> Path:
        return self.data_path / "exports"

    @property
    def db_path(self) -> Path:
        return self.data_path / "storybored.db"
