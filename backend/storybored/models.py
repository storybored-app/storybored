"""SQLModel tables — the whole data model per docs/CONTRACT.md."""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class Project(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    description: str = ""
    aspect_ratio: str = "16:9"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Scene(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    idx: int = 0
    title: str = ""
    slugline: str = ""
    description: str = ""


class Shot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    scene_id: int = Field(foreign_key="scene.id", index=True)
    idx: int = 0
    description: str = ""
    shot_type: str = ""  # free text: WIDE, MED, CU, ...
    camera: str = ""
    dialogue: str = ""
    duration_s: float = 4.0
    motion_prompt: str = ""  # for the video pass
    frame_position: str = "first"  # first | last — where the still anchors the clip
    status: str = "draft"  # draft | queued | generated | approved
    picked_take_id: int | None = None
    video_take_id: int | None = None


class Character(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    handle: str = Field(unique=True, index=True)  # no @ stored
    trigger: str = ""
    class_word: str = "person"
    lora_name: str = ""  # ComfyUI dropdown name incl. subdir
    lora_strength: float = 1.0
    thumbnail_path: str | None = None
    notes: str = ""
    status: str = "ready"  # ready | dataset | training | trained


class ShotCharacter(SQLModel, table=True):
    shot_id: int = Field(foreign_key="shot.id", primary_key=True)
    character_id: int = Field(foreign_key="character.id", primary_key=True)


class Take(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    shot_id: int = Field(foreign_key="shot.id", index=True)
    kind: str = "image"  # image | video
    status: str = "pending"  # pending | done | failed
    file_path: str | None = None
    thumb_path: str | None = None
    workflow_id: str = ""
    params_json: str = "{}"
    seed: int = 0
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    type: str  # image_gen | video_gen | animatic | dataset_prep | lora_train | lora_shootout
    status: str = "queued"  # queued | running | done | failed | cancelled
    lane: str = "gpu"
    payload_json: str = "{}"
    result_json: str | None = None
    error: str | None = None
    progress: float = 0.0
    detail: str = ""  # human-readable current step
    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
