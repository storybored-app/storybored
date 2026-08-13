import pytest
from fastapi.testclient import TestClient

from storybored.config import Settings
from storybored.main import create_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        comfy_mode_image_cmd="",
        comfy_mode_video_cmd="",
        comfy_flush_cmd="",
        llm_base_url="",
        lora_factory_dir="",
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c
