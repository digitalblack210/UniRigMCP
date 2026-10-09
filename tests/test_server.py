import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def write_glb(path, scene):
    body = json.dumps(scene).encode()
    body += b" " * (-len(body) % 4)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 20 + len(body)) + struct.pack("<I4s", len(body), b"JSON") + body)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(server, "JOBS", tmp_path / "jobs")
    monkeypatch.setenv("UNIRIG_MCP_ROOTS", str(allowed))
    return allowed


def test_reads_bones_with_parents(tmp_path):
    glb = tmp_path / "rat.glb"
    write_glb(glb, {"nodes": [{"name": "Armature", "children": [1]}, {"name": "bone_0", "children": [2, 3]},
                              {"name": "bone_1"}, {"name": "bone_2"}, {"name": "mesh"}],
                    "skins": [{"joints": [1, 2, 3]}]})
    assert server.read_bones(glb) == [{"name": "bone_0", "parent": None}, {"name": "bone_1", "parent": "bone_0"},
                                      {"name": "bone_2", "parent": "bone_0"}]


def test_unrigged_model_has_no_bones(tmp_path):
    glb = tmp_path / "plain.glb"
    write_glb(glb, {"nodes": [{"name": "mesh"}]})
    assert server.read_bones(glb) == []


def test_refuses_a_file_that_is_not_glb(tmp_path):
    fake = tmp_path / "fake.glb"
    fake.write_bytes(b"not a model at all, just twenty bytes")
    with pytest.raises(ValueError, match="not a GLB"):
        server.read_bones(fake)


def test_stage_is_the_last_one_announced():
    assert server.stage_from_log("") is None
    assert server.stage_from_log("=== skeleton 10:00:01\nnoise\n=== skin 10:00:40\n") == "skin"
    assert server.stage_from_log("=== skeleton 10:00:01\n=== nonsense 1\n") == "skeleton"


def test_model_outside_the_roots_is_refused(roots, tmp_path):
    outside = tmp_path / "elsewhere.glb"
    write_glb(outside, {})
    with pytest.raises(ValueError, match="outside the folders"):
        server.model_file(outside)


def test_dot_dot_cannot_escape_the_roots(roots, tmp_path):
    write_glb(tmp_path / "elsewhere.glb", {})
    with pytest.raises(ValueError, match="outside the folders"):
        server.model_file(roots / ".." / "elsewhere.glb")


def test_model_inside_the_roots_is_accepted(roots):
    inside = roots / "wolf.glb"
    write_glb(inside, {})
    assert server.model_file(inside) == inside.resolve()


def test_wrong_file_type_is_refused(roots):
    text = roots / "notes.txt"
    text.write_text("hello")
    with pytest.raises(ValueError, match="not a model file"):
        server.model_file(text)


def test_job_id_cannot_name_another_folder():
    with pytest.raises(ValueError, match="not a job id"):
        server.get_rig("../../somewhere")


def test_unknown_job_is_reported(roots):
    with pytest.raises(ValueError, match="no job"):
        server.get_rig("20260101-000000-abcdef", wait_seconds=0)


@pytest.mark.skipif(not server.ON_WINDOWS, reason="WSL path spelling only applies on Windows")
def test_windows_path_becomes_a_wsl_path():
    assert server.linux_path(Path(r"C:\Models\My Rat\rat.glb")) == "/mnt/c/Models/My Rat/rat.glb"


def test_a_stopped_job_fails_with_the_end_of_its_log(roots, monkeypatch):
    folder = server.JOBS / "20260101-000000-abcdef"
    folder.mkdir(parents=True)
    (folder / "rat_unirig.log").write_text("=== skeleton 10:00:01\nCUDA out of memory\n")
    state = {"id": folder.name, "status": "running", "stage": None, "name": "rat", "seed": 1, "source": "x", "pid": 1,
             "created": 0}
    server._write_state(state)
    monkeypatch.setattr(server, "_process_alive", lambda pid: False)
    out = server.get_rig(folder.name, wait_seconds=0)
    assert out["status"] == "failed" and out["stage"] == "skeleton"
    assert "CUDA out of memory" in out["error"]


def test_a_second_job_is_refused_while_one_runs(roots, monkeypatch):
    folder = server.JOBS / "20260101-000000-abcdef"
    folder.mkdir(parents=True)
    server._write_state({"id": folder.name, "status": "running", "stage": None, "name": "rat", "seed": 1, "source": "x",
                         "pid": 1, "created": 0})
    monkeypatch.setattr(server, "_process_alive", lambda pid: True)
    model = roots / "wolf.glb"
    write_glb(model, {})
    with pytest.raises(RuntimeError, match="still rigging"):
        server.start_rig(str(model))
