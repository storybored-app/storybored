# OWNED-BY: engine-agent
"""GET /api/workflows/{id}/graph — the workflow-export button.

Raw mode returns the pack's graph verbatim; effective mode applies the user's
engine_models / engine_loras customizations so the download matches what
StoryBored would submit. Both must stay importable API-format JSON.
"""

import json
from pathlib import Path

REPO_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


def pack_graph(pack_id: str) -> dict:
    return json.loads((REPO_WORKFLOWS / pack_id / "graph.json").read_text())


def test_raw_export_matches_pack_file(client):
    r = client.get("/api/workflows/krea2-basic/graph")
    assert r.status_code == 200
    assert 'filename="krea2-basic.api.json"' in r.headers["content-disposition"]
    assert r.json() == pack_graph("krea2-basic")


def test_unknown_pack_404(client):
    assert client.get("/api/workflows/nope/graph").status_code == 404


def test_effective_export_applies_model_swap(client):
    override = {"krea2-basic": {"unet": "my_custom_unet.safetensors"}}
    r = client.put(
        "/api/settings", json={"values": {"engine_models": json.dumps(override)}}
    )
    assert r.status_code == 200

    raw = client.get("/api/workflows/krea2-basic/graph").json()
    eff = client.get("/api/workflows/krea2-basic/graph?effective=true").json()

    manifest = json.loads(
        (REPO_WORKFLOWS / "krea2-basic" / "manifest.json").read_text()
    )
    slot = manifest["model_slots"][0]
    assert raw[slot["node"]]["inputs"][slot["input"]] != "my_custom_unet.safetensors"
    assert eff[slot["node"]]["inputs"][slot["input"]] == "my_custom_unet.safetensors"
    # everything else untouched
    eff[slot["node"]]["inputs"][slot["input"]] = raw[slot["node"]]["inputs"][slot["input"]]
    assert eff == raw
