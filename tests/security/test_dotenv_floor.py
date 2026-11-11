"""Dotenv files are read- and write-denied everywhere; templates stay readable (spec 015)."""


import pytest

from nanobot.security.protected_paths import ProtectedFloor, is_dotenv_name


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production", ".env.test"])
def test_dotenv_names_are_secrets(name):
    assert is_dotenv_name(name)


@pytest.mark.parametrize(
    "name", [".env.example", ".env.sample", ".env.template", ".env.dist", "env.txt", "my.env", ".envrc"],
)
def test_templates_and_lookalikes_are_not(name):
    assert not is_dotenv_name(name)


def test_floor_denies_dotenv_read_and_write_in_any_project(tmp_path):
    floor = ProtectedFloor(data_dir=tmp_path / "data", workspace=tmp_path / "ws")
    project = tmp_path / "projects" / "other"
    project.mkdir(parents=True)
    for name in (".env", ".env.local"):
        assert floor.matches(project / name, write=False)
        assert floor.matches(project / name, write=True)
    assert not floor.matches(project / ".env.example", write=False)
    assert not floor.matches(project / "README.md", write=True)
