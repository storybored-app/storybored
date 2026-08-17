# OWNED-BY: lifecycle-agent
"""The `.storybored` project archive: export job handler + import logic.

A `.storybored` file is a plain zip:

    manifest.json          # schema_version, app_version, board payload,
                           # soft character references, workflow-pack notes
    media/{pid}/{sid}/...  # the project's takes (stills, clips, thumbs),
                           # DATA_DIR-relative paths exactly as stored in the DB
    exports/{pid}/*.mp4    # finished animatics
    workflows/{id}/...     # user-installed engine packs referenced by takes
                           # (repo-shipped packs are noted by id, never bundled)

Characters travel as SOFT references (handle, trigger, class word, LoRA *name*
and strength) — LoRA weight files are never bundled; the importing machine must
have them installed in its engine. Import is a two-pass ID remap (project/
scenes/shots, then takes, then the shots' pick pointers) with every extracted
member zip-slip-guarded via the resolve + is_relative_to idiom.
"""

import asyncio
import json
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select

from storybored.api.projects import board_payload
from storybored.api.shots import MENTION_RE, refresh_shot_characters
from storybored.config import Settings
from storybored.engine import registry
from storybored.jobs.registry import register
from storybored.jobs.runner import JobCancelled
from storybored.models import Character, Project, Scene, Shot, ShotCharacter, Take

#: bump when the manifest/layout changes incompatibly; import refuses newer
SCHEMA_VERSION = 1
ARCHIVE_SUFFIX = ".storybored"

#: character rows travel with these fields (soft references — no weight files)
CHARACTER_FIELDS = (
    "name",
    "handle",
    "trigger",
    "class_word",
    "lora_name",
    "lora_strength",
    "notes",
    "status",
)


def app_version() -> str:
    try:
        return metadata.version("storybored")
    except metadata.PackageNotFoundError:  # running from a bare checkout
        return "0.0.0"


def archive_path(settings: Settings, project_id: int) -> Path:
    return settings.exports_path / str(project_id) / f"project-{project_id}{ARCHIVE_SUFFIX}"


# -- export -------------------------------------------------------------------


def _project_character_rows(session: Session, payload: dict) -> list[Character]:
    shot_ids = [
        shot["id"] for scene in payload.get("scenes", []) for shot in scene.get("shots", [])
    ]
    if not shot_ids:
        return []
    char_ids = {
        link.character_id
        for link in session.exec(
            select(ShotCharacter).where(ShotCharacter.shot_id.in_(shot_ids))  # type: ignore[attr-defined]
        )
    }
    if not char_ids:
        return []
    return list(
        session.exec(select(Character).where(Character.id.in_(char_ids)))  # type: ignore[attr-defined]
    )


def _referenced_workflow_ids(payload: dict) -> set[str]:
    return {
        take["workflow_id"]
        for scene in payload.get("scenes", [])
        for shot in scene.get("shots", [])
        for take in shot.get("takes", [])
        if take.get("workflow_id")
    }


def build_manifest(session: Session, settings: Settings, project: Project) -> dict:
    """The archive manifest + the bundle plan (media files, packs to include)."""
    payload = board_payload(session, project)
    characters = [
        {
            "id": c.id,
            **{f: getattr(c, f) for f in CHARACTER_FIELDS},
            # thumbnails are machine-local media — soft reference only
            "thumbnail_path": c.thumbnail_path,
        }
        for c in _project_character_rows(session, payload)
    ]

    packs = registry.load_packs(settings)
    user_workflows_dir = (settings.data_path / "workflows").resolve()
    builtin: list[str] = []
    bundled: list[str] = []
    not_installed: list[str] = []
    for wid in sorted(_referenced_workflow_ids(payload)):
        pack = packs.get(wid)
        if pack is None:
            not_installed.append(wid)
        elif pack.dir.resolve().is_relative_to(user_workflows_dir):
            bundled.append(wid)
        else:
            builtin.append(wid)

    return {
        "format": "storybored-project",
        "schema_version": SCHEMA_VERSION,
        "app_version": app_version(),
        "exported_at": datetime.now(UTC).isoformat(),
        "project": payload,
        "characters": characters,
        "workflow_packs": {
            "builtin": builtin,  # shipped with StoryBored — noted, not bundled
            "bundled": bundled,  # user packs included under workflows/ in the zip
            "not_installed": not_installed,
        },
        "notes": {
            "loras": "Character LoRA weight files are NOT bundled — install "
            "the referenced files in your engine before generating.",
        },
    }


def _bundle_files(settings: Settings, manifest: dict) -> list[tuple[Path, str]]:
    """(absolute source, arcname) for every file going into the zip."""
    out: list[tuple[Path, str]] = []
    data = settings.data_path
    payload = manifest["project"]
    project_id = payload["id"]

    seen: set[str] = set()
    for scene in payload.get("scenes", []):
        for shot in scene.get("shots", []):
            for take in shot.get("takes", []):
                for rel in (take.get("file_path"), take.get("thumb_path")):
                    if not rel or rel in seen:
                        continue
                    seen.add(rel)
                    src = (data / rel).resolve()
                    if src.is_relative_to(data) and src.is_file():
                        out.append((src, rel))

    export_dir = settings.exports_path / str(project_id)
    if export_dir.is_dir():
        for src in sorted(export_dir.iterdir()):
            if src.is_file() and src.suffix == ".mp4":
                out.append((src, str(src.relative_to(data))))

    packs = registry.load_packs(settings)
    for pack_id in manifest["workflow_packs"]["bundled"]:
        pack = packs.get(pack_id)
        if pack is None:
            continue
        for src in sorted(p for p in pack.dir.rglob("*") if p.is_file()):
            out.append((src, f"workflows/{pack_id}/{src.relative_to(pack.dir).as_posix()}"))
    return out


@register("project_export")
async def project_export(job, ctx):
    """Write DATA_DIR/exports/{id}/project-{id}.storybored for the project."""
    payload = json.loads(job.payload_json or "{}")
    project_id = payload.get("project_id")
    settings: Settings = ctx.settings

    with ctx.session_factory() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise RuntimeError(f"project {project_id} not found")
        ctx.update_progress(progress=0.0, detail="collecting project")
        manifest = build_manifest(session, settings, project)

    files = _bundle_files(settings, manifest)
    dest = archive_path(settings, project_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    def write_zip() -> None:
        total = len(files) + 1
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps(manifest, indent=2))
                for i, (src, arcname) in enumerate(files):
                    if ctx.cancelled():
                        raise JobCancelled(f"job {ctx.job_id} cancelled")
                    ctx.update_progress(
                        progress=i / total, detail=f"packing {i + 1}/{len(files)} files"
                    )
                    zf.write(src, arcname)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)

    await asyncio.to_thread(write_zip)
    ctx.update_progress(progress=1.0, detail="archive ready")
    return {
        "file_path": str(dest.relative_to(settings.data_path)),
        "size_bytes": dest.stat().st_size,
        "files": len(files),
        "download_url": f"/api/projects/{project_id}/export/download",
    }


# -- import -------------------------------------------------------------------


class ArchiveError(ValueError):
    """Invalid/unsupported archive — surfaced to the API as a 400."""


@dataclass
class ImportResult:
    project: Project
    warnings: list[str] = field(default_factory=list)
    characters_linked: list[str] = field(default_factory=list)
    characters_created: list[str] = field(default_factory=list)
    characters_renamed: dict[str, str] = field(default_factory=dict)
    #: the created Character rows (for SSE publication after commit)
    created_rows: list[Character] = field(default_factory=list)


def _read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        raw = zf.read("manifest.json")
    except KeyError:
        raise ArchiveError("not a StoryBored project archive (no manifest.json)") from None
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise ArchiveError("archive manifest is not valid JSON") from None
    if not isinstance(manifest, dict):
        raise ArchiveError("archive manifest is not a JSON object")
    version = manifest.get("schema_version")
    if not isinstance(version, int) or version < 1:
        raise ArchiveError("archive has no valid schema_version")
    if version > SCHEMA_VERSION:
        raise ArchiveError(
            f"archive schema_version {version} is newer than this StoryBored "
            f"(supports up to {SCHEMA_VERSION}) — update StoryBored to import it"
        )
    if not isinstance(manifest.get("project"), dict):
        raise ArchiveError("archive manifest has no project payload")
    return manifest


def _safe_dest(base: Path, *parts: str) -> Path:
    """Resolve base/parts and refuse anything escaping base (zip-slip guard)."""
    dest = (base.joinpath(*parts)).resolve()
    if not dest.is_relative_to(base.resolve()):
        raise ArchiveError("archive contains an unsafe path — import aborted")
    return dest


def _extract_member(zf: zipfile.ZipFile, member: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)


def _import_characters(
    session: Session, manifest: dict, mode: str, result: ImportResult
) -> dict[str, Character]:
    """Resolve archive characters to rows: link, create, or rename per mode.

    Returns old-handle → Character (for mention rewriting + linking)."""
    existing = {
        c.handle: c for c in session.exec(select(Character)) if c.handle
    }
    taken = set(existing)
    resolved: dict[str, Character] = {}
    for entry in manifest.get("characters") or []:
        handle = str(entry.get("handle") or "").lstrip("@").lower()
        if not handle:
            continue
        if handle in existing and mode == "merge":
            resolved[handle] = existing[handle]
            result.characters_linked.append(handle)
            continue
        new_handle = handle
        if handle in taken:  # rename mode collision → suffixed handle (ava → ava2)
            n = 2
            while f"{handle}{n}" in taken:
                n += 1
            new_handle = f"{handle}{n}"
            result.characters_renamed[handle] = new_handle
        status = str(entry.get("status") or "ready")
        char = Character(
            name=str(entry.get("name") or new_handle),
            handle=new_handle,
            trigger=str(entry.get("trigger") or ""),
            class_word=str(entry.get("class_word") or "person"),
            lora_name=str(entry.get("lora_name") or ""),
            lora_strength=float(entry.get("lora_strength") or 1.0),
            notes=str(entry.get("notes") or ""),
            # mid-pipeline states can't survive the move (their training jobs
            # live on the exporting machine) — only "trained" carries over
            status="trained" if status == "trained" else "ready",
        )
        session.add(char)
        taken.add(new_handle)
        resolved[handle] = char
        result.characters_created.append(new_handle)
        result.created_rows.append(char)
        if char.lora_name:
            result.warnings.append(
                f"@{new_handle} references LoRA '{char.lora_name}' — LoRA files "
                "are not bundled; install it in your engine before generating"
            )
    session.flush()
    return resolved


def _mention_rewriter(renamed: dict[str, str]):
    if not renamed:
        return lambda text: text

    def rewrite(text: str) -> str:
        def repl(match):
            new = renamed.get(match.group(1).lower())
            return f"@{new}" if new else match.group(0)

        return MENTION_RE.sub(repl, text or "")

    return rewrite


def _take_dest_name(new_take_id: int, old_rel: str, thumb: bool) -> str:
    suffix = Path(old_rel).suffix or ".png"
    return f"take_{new_take_id}_thumb.png" if thumb else f"take_{new_take_id}{suffix}"


def import_archive(
    session: Session, settings: Settings, zf: zipfile.ZipFile, mode: str
) -> ImportResult:
    """Create a brand-new project from the archive. Caller commits/rolls back.

    Two passes: project → scenes → shots (IDs remapped), then takes, then the
    shots' picked_take_id / video_take_id are patched to the new take ids.
    Media is extracted under the NEW ids; every write is zip-slip guarded.
    Missing LoRAs / packs are warnings, never failures. On any error the
    already-extracted media/exports trees of the half-imported project are
    removed (the caller rolls the DB rows back)."""
    from storybored.api.projects import remove_project_trees

    holder: dict[str, int] = {}
    try:
        return _import_archive(session, settings, zf, mode, holder)
    except BaseException:
        pid = holder.get("project_id")
        if pid is not None:
            remove_project_trees(settings, pid)
        raise


def _import_archive(
    session: Session,
    settings: Settings,
    zf: zipfile.ZipFile,
    mode: str,
    holder: dict[str, int],
) -> ImportResult:
    manifest = _read_manifest(zf)
    members = set(zf.namelist())
    src_project = manifest["project"]
    old_project_id = src_project.get("id")

    result = ImportResult(project=None)  # type: ignore[arg-type]
    # characters first: links happen later via refresh_shot_characters, which
    # matches the (possibly rewritten) @mentions against these rows
    _import_characters(session, manifest, mode, result)
    rewrite = _mention_rewriter(result.characters_renamed)

    project = Project(
        title=str(src_project.get("title") or "Imported project"),
        description=str(src_project.get("description") or ""),
        aspect_ratio=str(src_project.get("aspect_ratio") or "16:9"),
    )
    session.add(project)
    session.flush()
    result.project = project
    holder["project_id"] = project.id

    # pass 1: scenes + shots (remember each shot's old pick pointers)
    shots_by_old_id: dict[int, Shot] = {}
    old_pointers: dict[int, tuple[int | None, int | None]] = {}
    old_takes: list[tuple[int, dict]] = []  # (old_shot_id, take payload)
    for scene_src in sorted(src_project.get("scenes") or [], key=lambda s: s.get("idx", 0)):
        scene = Scene(
            project_id=project.id,
            idx=int(scene_src.get("idx") or 0),
            title=str(scene_src.get("title") or ""),
            slugline=str(scene_src.get("slugline") or ""),
            description=str(scene_src.get("description") or ""),
        )
        session.add(scene)
        session.flush()
        for shot_src in sorted(scene_src.get("shots") or [], key=lambda s: s.get("idx", 0)):
            shot = Shot(
                scene_id=scene.id,
                idx=int(shot_src.get("idx") or 0),
                description=rewrite(str(shot_src.get("description") or "")),
                shot_type=str(shot_src.get("shot_type") or ""),
                camera=str(shot_src.get("camera") or ""),
                dialogue=str(shot_src.get("dialogue") or ""),
                duration_s=float(shot_src.get("duration_s") or 4.0),
                motion_prompt=rewrite(str(shot_src.get("motion_prompt") or "")),
                frame_position=str(shot_src.get("frame_position") or "first"),
                status=str(shot_src.get("status") or "draft"),
            )
            session.add(shot)
            session.flush()
            old_shot_id = shot_src.get("id")
            if old_shot_id is not None:
                shots_by_old_id[old_shot_id] = shot
                old_pointers[old_shot_id] = (
                    shot_src.get("picked_take_id"),
                    shot_src.get("video_take_id"),
                )
            for take_src in shot_src.get("takes") or []:
                old_takes.append((old_shot_id, take_src))

    # pass 2: takes — new rows + media extracted under the new ids
    take_id_map: dict[int, int] = {}
    for old_shot_id, take_src in old_takes:
        shot = shots_by_old_id.get(old_shot_id)
        if shot is None:
            continue
        take = Take(
            shot_id=shot.id,
            kind=str(take_src.get("kind") or "image"),
            status=str(take_src.get("status") or "failed"),
            workflow_id=str(take_src.get("workflow_id") or ""),
            params_json=str(take_src.get("params_json") or "{}"),
            seed=int(take_src.get("seed") or 0),
            error=take_src.get("error"),
        )
        session.add(take)
        session.flush()
        if take_src.get("id") is not None:
            take_id_map[take_src["id"]] = take.id
        for attr, thumb in (("file_path", False), ("thumb_path", True)):
            old_rel = take_src.get(attr)
            if not old_rel:
                continue
            if old_rel not in members:
                if take.status == "done" and not thumb:
                    result.warnings.append(
                        f"media file '{old_rel}' is missing from the archive — "
                        "the take was imported without it"
                    )
                continue
            dest = _safe_dest(
                settings.media_path,
                str(project.id),
                str(shot.id),
                _take_dest_name(take.id, old_rel, thumb),
            )
            _extract_member(zf, old_rel, dest)
            setattr(take, attr, str(dest.relative_to(settings.data_path)))
        session.add(take)

    # patch the shots' pick pointers onto the new take ids
    for old_shot_id, (old_pick, old_video) in old_pointers.items():
        shot = shots_by_old_id[old_shot_id]
        shot.picked_take_id = take_id_map.get(old_pick) if old_pick else None
        shot.video_take_id = take_id_map.get(old_video) if old_video else None
        if shot.status == "queued":  # a mid-generation export has no job here
            shot.status = "generated" if shot.picked_take_id else "draft"
        session.add(shot)

    # shotcharacter links from the (rewritten) descriptions
    session.flush()
    for shot in shots_by_old_id.values():
        refresh_shot_characters(session, shot)

    # finished animatics travel along
    if old_project_id is not None:
        prefix = f"exports/{old_project_id}/"
        for member in sorted(members):
            if member.startswith(prefix) and member.endswith(".mp4"):
                dest = _safe_dest(
                    settings.exports_path, str(project.id), Path(member).name
                )
                _extract_member(zf, member, dest)

    # bundled user workflow packs — only when the id isn't already present
    packs_present = set(registry.load_packs(settings))
    pack_info = manifest.get("workflow_packs") or {}
    for pack_id in pack_info.get("bundled") or []:
        prefix = f"workflows/{pack_id}/"
        if pack_id in packs_present:
            result.warnings.append(
                f"engine pack '{pack_id}' is already installed — the bundled copy was ignored"
            )
            continue
        pack_members = [m for m in sorted(members) if m.startswith(prefix) and not m.endswith("/")]
        for member in pack_members:
            dest = _safe_dest(settings.data_path, *Path(member).parts)
            _extract_member(zf, member, dest)
    for pack_id in list(pack_info.get("builtin") or []) + list(
        pack_info.get("not_installed") or []
    ):
        if pack_id not in packs_present:
            result.warnings.append(
                f"engine pack '{pack_id}' is not installed here — takes made with "
                "it are kept, but re-generating needs the pack"
            )

    return result


def import_result_payload(result: ImportResult) -> dict:
    return {
        "project": jsonable_encoder(result.project),
        "warnings": result.warnings,
        "characters": {
            "linked": result.characters_linked,
            "created": result.characters_created,
            "renamed": result.characters_renamed,
        },
    }
