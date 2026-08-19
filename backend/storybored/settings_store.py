"""Runtime setting resolution: DB-backed overrides on top of env config.

A `setting` row's value wins over the corresponding env var when set. This
module is import-light on purpose (no FastAPI) so engine handlers, the job
runner, and the LLM client can resolve settings without pulling in routing.
The HTTP surface for editing settings lives in `api/settings_api.py`.
"""

from sqlmodel import Session

from storybored.config import Settings
from storybored.models import Setting


def resolve_setting(
    session: Session, settings: Settings, key: str
) -> tuple[str, str]:
    """Return (value, source) — source is 'override' (DB), 'env', or 'unset'."""
    row = session.get(Setting, key)
    if row is not None and row.value:
        return row.value, "override"
    env_val = str(getattr(settings, key, "") or "")
    if env_val:
        return env_val, "env"
    return "", "unset"


def effective_setting(session: Session, settings: Settings, key: str) -> str:
    """DB override if present and non-empty, else env value, else ""."""
    return resolve_setting(session, settings, key)[0]
