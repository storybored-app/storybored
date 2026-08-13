"""Demo project seed: "The Last Lighthouse" — an original two-scene mini
script written fresh for this repo. No characters, no generated media."""

from fastapi import APIRouter, Depends
from sqlmodel import Session

from storybored.api.projects import board_payload
from storybored.db import get_session
from storybored.models import Project, Scene, Shot

router = APIRouter(prefix="/api", tags=["demo"])

DEMO_TITLE = "The Last Lighthouse"
DEMO_DESCRIPTION = (
    "A keeper, a dying lamp, and one storm too many. "
    "Demo project — play with takes, approvals and the animatic export."
)

DEMO_SCENES = [
    {
        "title": "The Warning",
        "slugline": "EXT. GULL ROCK LIGHTHOUSE - DUSK",
        "description": "The last supply run of the season arrives ahead of a storm front.",
        "shots": [
            {
                "shot_type": "WIDE",
                "duration_s": 5.0,
                "camera": "slow push-in from the sea",
                "description": (
                    "A lone lighthouse on a jagged headland, its white tower catching "
                    "the last copper light while a wall of storm cloud piles up on the "
                    "horizon."
                ),
            },
            {
                "shot_type": "MED",
                "duration_s": 4.0,
                "camera": "handheld, tracking",
                "description": (
                    "Marisol, the keeper, in salt-stained oilskins, wrestles a supply "
                    "crate through the iron door as the first fat raindrops hit the "
                    "stone."
                ),
            },
            {
                "shot_type": "CU",
                "duration_s": 3.0,
                "camera": "locked off",
                "description": (
                    "Her weathered hands strike a match inside the lamp housing; the "
                    "flame gutters, nearly dies, then steadies."
                ),
                "dialogue": "Come on. Don't quit on me now.",
            },
        ],
    },
    {
        "title": "The Longest Night",
        "slugline": "INT. LANTERN ROOM - NIGHT",
        "description": "The storm hits at full strength; the lamp is all that stands between "
        "the fleet and the rocks.",
        "shots": [
            {
                "shot_type": "WIDE",
                "duration_s": 5.0,
                "camera": "slow orbit",
                "description": (
                    "Rain hammers the lantern-room glass. The great lens turns, "
                    "throwing its beam out into the churning dark."
                ),
            },
            {
                "shot_type": "CU",
                "duration_s": 4.0,
                "camera": "static, beam sweeping across frame",
                "description": (
                    "Marisol's face, lit then shadowed by the rotating beam, eyes fixed "
                    "on the sea."
                ),
                "dialogue": "Hold together, old girl. One more night.",
            },
            {
                "shot_type": "WIDE",
                "duration_s": 6.0,
                "camera": "high angle from the gallery rail",
                "description": (
                    "Far below, a small fishing boat crests a black swell, catches the "
                    "sweep of the beam, and turns toward safe water."
                ),
            },
        ],
    },
]


def create_demo(session: Session) -> Project:
    project = Project(title=DEMO_TITLE, description=DEMO_DESCRIPTION, aspect_ratio="16:9")
    session.add(project)
    session.flush()
    for scene_idx, scene_spec in enumerate(DEMO_SCENES):
        scene = Scene(
            project_id=project.id,
            idx=scene_idx,
            title=scene_spec["title"],
            slugline=scene_spec["slugline"],
            description=scene_spec["description"],
        )
        session.add(scene)
        session.flush()
        for shot_idx, shot_spec in enumerate(scene_spec["shots"]):
            session.add(
                Shot(
                    scene_id=scene.id,
                    idx=shot_idx,
                    status="draft",
                    description=shot_spec["description"],
                    shot_type=shot_spec.get("shot_type", ""),
                    camera=shot_spec.get("camera", ""),
                    dialogue=shot_spec.get("dialogue", ""),
                    duration_s=shot_spec.get("duration_s", 4.0),
                )
            )
    session.commit()
    session.refresh(project)
    return project


@router.post("/demo", status_code=201)
def load_demo(session: Session = Depends(get_session)):
    project = create_demo(session)
    # Return the full nested board (scenes → shots → takes) so the UI can render
    # the loaded demo immediately without a follow-up GET.
    return board_payload(session, project)
