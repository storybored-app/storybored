"""API request/response models."""

from typing import Literal

from pydantic import BaseModel


class ProjectCreate(BaseModel):
    title: str
    description: str = ""
    aspect_ratio: str = "16:9"


class ProjectUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    aspect_ratio: str | None = None
    continuity_enabled: bool | None = None


class SceneCreate(BaseModel):
    title: str = ""
    slugline: str = ""
    description: str = ""
    look: str = ""


class SceneUpdate(BaseModel):
    title: str | None = None
    slugline: str | None = None
    description: str | None = None
    look: str | None = None
    #: master plate: a finished image take from this scene (0/null clears)
    plate_take_id: int | None = None


class SceneReorder(BaseModel):
    scene_ids: list[int]


class ShotCreate(BaseModel):
    description: str = ""
    shot_type: str = ""
    camera: str = ""
    dialogue: str = ""
    duration_s: float = 4.0
    motion_prompt: str = ""


class ShotUpdate(BaseModel):
    description: str | None = None
    shot_type: str | None = None
    camera: str | None = None
    dialogue: str | None = None
    duration_s: float | None = None
    motion_prompt: str | None = None
    frame_position: Literal["first", "last"] | None = None
    #: hold-to-scene-plate strength ("" = off)
    plate_hold: Literal["", "loose", "medium", "tight"] | None = None


class ShotReorder(BaseModel):
    shot_ids: list[int]


class SettingsUpdate(BaseModel):
    """PUT /api/settings body: key -> value; null/empty value clears the override."""

    values: dict[str, str | None]
