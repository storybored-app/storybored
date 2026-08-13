# OWNED-BY: engine-agent
"""ComfyClient against the fake server: submit/poll/download, errors, caching."""

import pytest
from fake_comfy import fake_comfy  # noqa: F401 - fixture

from storybored.engine.comfy_client import (
    ComfyClient,
    ComfyError,
    clear_object_info_cache,
)

GRAPH = {
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "storybored/take_1", "images": ["8", 0]},
    }
}


async def test_submit_wait_download(fake_comfy, tmp_path):  # noqa: F811
    client = ComfyClient(fake_comfy.url)
    prompt_id = await client.submit(GRAPH)
    assert fake_comfy.state.prompts[prompt_id] == GRAPH

    entry = await client.wait_for(prompt_id, poll_interval=0.01)
    images = entry["outputs"]["9"]["images"]
    assert images[0]["filename"] == "take_1_00001_.png"
    assert images[0]["subfolder"] == "storybored"

    dest = tmp_path / "out" / "take_1.png"
    data = await client.download(
        images[0]["filename"], images[0]["subfolder"], images[0]["type"], dest
    )
    assert dest.read_bytes() == data == fake_comfy.state.png


async def test_queue_position_and_delayed_completion(fake_comfy):  # noqa: F811
    fake_comfy.state.polls_before_done = 2
    client = ComfyClient(fake_comfy.url)
    prompt_id = await client.submit(GRAPH)
    assert await client.queue_position(prompt_id) == 0  # "running"
    positions: list = []
    entry = await client.wait_for(
        prompt_id, poll_interval=0.01, on_status=positions.append
    )
    assert entry["status"]["completed"] is True
    assert positions  # empty polls reported a queue position


async def test_submit_node_errors_extracted(fake_comfy):  # noqa: F811
    fake_comfy.state.fail_submit_error = "CUDA out of memory"
    client = ComfyClient(fake_comfy.url)
    with pytest.raises(ComfyError) as err:
        await client.submit(GRAPH)
    msg = str(err.value)
    assert "CUDA out of memory" in msg
    assert "node 3" in msg


async def test_unreachable_is_comfy_error():
    client = ComfyClient("http://127.0.0.1:9", timeout=0.5)
    with pytest.raises(ComfyError, match="engine unreachable"):
        await client.submit(GRAPH)


async def test_object_info_cached_60s(fake_comfy):  # noqa: F811
    client = ComfyClient(fake_comfy.url)
    first = await client.model_enum("LoraLoader", "lora_name")
    assert "characters/hero_v1.safetensors" in first
    assert "(Krea 2) 8-Step Turbo Distill Rank 64 V2026.1.safetensors" in first
    again = await client.model_enum("LoraLoader", "lora_name")
    assert again == first
    assert fake_comfy.state.request_counts["/object_info"] == 1  # cache hit

    # unknown class → empty enum, and cache can be cleared explicitly
    assert await client.model_enum("NoSuchLoader", "x") == []
    clear_object_info_cache()
    await client.model_enum("LoraLoader", "lora_name")
    assert fake_comfy.state.request_counts["/object_info"] == 3


async def test_upload_image(fake_comfy):  # noqa: F811
    client = ComfyClient(fake_comfy.url)
    result = await client.upload_image(b"png-bytes", "first_frame.png")
    assert result["name"] == "first_frame.png"
    assert fake_comfy.state.uploads == ["first_frame.png"]


async def test_cancel_best_effort(fake_comfy):  # noqa: F811
    client = ComfyClient(fake_comfy.url)
    prompt_id = await client.submit(GRAPH)
    await client.cancel(prompt_id)
    assert fake_comfy.state.deleted == [prompt_id]
    assert fake_comfy.state.interrupts == 1
