"""Shot casting: keep the shotcharacter link table in sync with @handle
mentions in shot descriptions. Shared by the shot/breakdown APIs, archive
import, and the image engine's post-render refresh."""

import re

from sqlmodel import Session, select

from storybored.models import Character, Shot, ShotCharacter

MENTION_RE = re.compile(r"@([A-Za-z0-9_\-]+)")


def refresh_shot_characters(session: Session, shot: Shot) -> None:
    """Sync the shotcharacter link table from @handle mentions in the description.

    Mentions are lowercased before matching because handles are stored lowercase
    (aligns with the engine's lowercase-only MENTION_RE), so ``@TestChar`` casts
    the same character as ``@testchar``."""
    handles = {m.lower() for m in MENTION_RE.findall(shot.description or "")}
    matched_ids: set[int] = set()
    if handles:
        chars = session.exec(
            select(Character).where(Character.handle.in_(handles))  # type: ignore[attr-defined]
        ).all()
        matched_ids = {c.id for c in chars if c.id is not None}
    existing = session.exec(
        select(ShotCharacter).where(ShotCharacter.shot_id == shot.id)
    ).all()
    for link in existing:
        if link.character_id not in matched_ids:
            session.delete(link)
    existing_ids = {link.character_id for link in existing}
    for cid in matched_ids - existing_ids:
        session.add(ShotCharacter(shot_id=shot.id, character_id=cid))
