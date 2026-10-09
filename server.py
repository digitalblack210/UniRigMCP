"""UniRig as MCP tools: an agent hands over a 3D model and gets it back with a skeleton.

Run by an MCP client (Claude Code, Codex, anything that speaks the protocol) over stdio:

    python server.py

UniRig itself runs on Linux with an NVIDIA GPU. On Windows this server starts it
inside WSL; on Linux it starts it directly. Either way UniRig must already be
installed with scripts/install.sh.

Three things keep the server in its lane:

  - It reads models only from folders named in UNIRIG_MCP_ROOTS, and copies
    results only into those folders. An agent cannot use it to read or write
    anywhere else on the machine.
  - One model is rigged at a time. The GPU is the limit, not the server.
  - Nothing is printed to stdout but the protocol. A stray print would corrupt
    the stream the client is reading.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS = Path(os.getenv("UNIRIG_MCP_JOBS", ROOT / "jobs")).resolve()
RIG_SCRIPT = ROOT / "scripts" / "rig.sh"
MODEL_TYPES = {".glb", ".gltf", ".fbx", ".obj", ".vrm"}
STAGES = ("skeleton", "skin", "merge", "done")
DEFAULT_SEED = 12345
MAX_WAIT = 50   # seconds one call may block; MCP clients give up on a tool that takes much longer
ON_WINDOWS = os.name == "nt"


def allowed_roots():
    """The job folder, plus every folder in UNIRIG_MCP_ROOTS (separated by ; or the OS separator)."""
    raw = os.getenv("UNIRIG_MCP_ROOTS", "")
    parts = raw.replace(os.pathsep, ";").split(";")
    return [JOBS] + [Path(part.strip()).expanduser().resolve() for part in parts if part.strip()]


def inside_allowed(path):
    """The path, resolved, if it sits under an allowed folder. Otherwise ValueError."""
    resolved = Path(path).expanduser().resolve()
    roots = allowed_roots()
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise ValueError("%s is outside the folders this server may use: %s. Add its folder to UNIRIG_MCP_ROOTS."
                         % (path, ", ".join(map(str, roots))))
    return resolved


def model_file(path):
    resolved = inside_allowed(path)
    if resolved.suffix.lower() not in MODEL_TYPES or not resolved.is_file():
        raise ValueError("%s is not a model file that exists (%s)." % (path, ", ".join(sorted(MODEL_TYPES))))
    return resolved


def linux_path(path):
    """How the path is spelled where UniRig runs: /mnt/c/... inside WSL, unchanged on Linux."""
    path = Path(path)
    if not ON_WINDOWS:
        return str(path)
    drive = path.drive.rstrip(":")
    if len(drive) != 1:
        raise ValueError("%s is not on a lettered drive, so WSL cannot reach it." % path)
    return "/mnt/%s/%s" % (drive.lower(), "/".join(path.parts[1:]))


def read_bones(glb_path):
    """The skeleton stored in a GLB, as [{"name", "parent"}], without needing Blender.

    Reads only the JSON chunk at the front of the file. Returns [] for a model with no skin.
    """
    with open(glb_path, "rb") as handle:
        magic, _version, _length = struct.unpack("<4sII", handle.read(12))
        if magic != b"glTF":
            raise ValueError("%s is not a GLB file." % glb_path)
        chunk_length, chunk_type = struct.unpack("<I4s", handle.read(8))
        if chunk_type != b"JSON":
            raise ValueError("%s does not start with a JSON chunk." % glb_path)
        scene = json.loads(handle.read(chunk_length))
    nodes = scene.get("nodes", [])
    joints = []
    for skin in scene.get("skins", []):
        joints.extend(index for index in skin.get("joints", []) if index not in joints)
    parent_of = {child: index for index, node in enumerate(nodes) for child in node.get("children", [])}
    wanted = set(joints)
    bones = []
    for index in joints:
        parent = parent_of.get(index)
        bones.append({"name": nodes[index].get("name", "node_%d" % index),
                      "parent": nodes[parent].get("name", "node_%d" % parent) if parent in wanted else None})
    return bones


def stage_from_log(text):
    """The last stage the rig script announced: 'skeleton', 'skin', 'merge' or 'done'. None before the first."""
    stage = None
    for line in text.splitlines():
        if line.startswith("=== "):
            word = line[4:].split(" ", 1)[0]
            if word in STAGES:
                stage = word
    return stage


def _state_path(job_id):
    if not job_id or any(char not in "0123456789abcdef-" for char in job_id):
        raise ValueError("%r is not a job id this server made." % job_id)
    return JOBS / job_id / "job.json"


def _read_state(job_id):
    path = _state_path(job_id)
    if not path.is_file():
        raise ValueError("There is no job %s." % job_id)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(state):
    path = _state_path(state["id"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _process_alive(pid):
    if ON_WINDOWS:
        result = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV"], capture_output=True,
                                text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return '"%d"' % pid in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _refresh(state):
    """Bring a job's recorded status up to date with what is on disk. Saves the state if it changed."""
    if state["status"] != "running":
        return state
    folder = JOBS / state["id"]
    log = folder / (state["name"] + "_unirig.log")
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    stage = stage_from_log(text)
    rigged = folder / (state["name"] + "_rigged.glb")
    if stage == "done" and rigged.is_file():
        # When the file was written, not when somebody next asked: a job may sit unpolled.
        state.update(status="done", stage="done", finished=rigged.stat().st_mtime)
    elif not _process_alive(state["pid"]):
        # The script stopped without reaching "done". Its log is the only account of why.
        tail = [line for line in text.splitlines() if line.strip()][-12:]
        state.update(status="failed", stage=stage, finished=log.stat().st_mtime if log.is_file() else time.time(),
                     error="UniRig stopped during the %s stage. Last lines of its log:\n%s"
                           % (stage or "start-up", "\n".join(tail) or "(the log is empty)"))
    else:
        if stage == state.get("stage"):
            return state
        state["stage"] = stage
    _write_state(state)
    return state


def _all_jobs():
    jobs = []
    if JOBS.is_dir():
        for path in JOBS.glob("*/job.json"):
            try:
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue   # a half-written or foreign folder is not a job
    return sorted(jobs, key=lambda job: job.get("created", 0), reverse=True)


def _describe(state):
    """What an agent needs to know about a job, with real paths once there is something at them."""
    folder = JOBS / state["id"]
    end = state.get("finished") or time.time()
    out = {"job_id": state["id"], "status": state["status"], "stage": state.get("stage"), "name": state["name"],
           "seed": state["seed"], "source_model": state["source"], "elapsed_seconds": round(end - state["created"]),
           "error": state.get("error")}
    files = {"rigged_glb": folder / (state["name"] + "_rigged.glb"), "skeleton_fbx": folder / (state["name"] + "_skeleton.fbx"),
             "skin_fbx": folder / (state["name"] + "_skin.fbx"), "log": folder / (state["name"] + "_unirig.log")}
    out["files"] = {kind: str(path) for kind, path in files.items() if path.is_file()}
    if state["status"] == "done":
        try:
            bones = read_bones(files["rigged_glb"])
        except (OSError, ValueError) as exc:
            out["bones_error"] = str(exc)
        else:
            out["bone_count"] = len(bones)
            out["bones"] = bones
    return out


def start_rig(model, seed=None):
    source = model_file(model)
    if not RIG_SCRIPT.is_file():
        raise RuntimeError("%s is missing, so there is nothing to run." % RIG_SCRIPT)
    for job in _all_jobs():
        if _refresh(job)["status"] == "running":
            raise RuntimeError("Job %s is still rigging %s. One model is rigged at a time: wait for it, then try again."
                               % (job["id"], job["name"]))
    seed = DEFAULT_SEED if seed is None else int(seed)
    job_id = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), os.urandom(3).hex())
    folder = JOBS / job_id
    folder.mkdir(parents=True)
    arguments = ["bash", linux_path(RIG_SCRIPT), linux_path(source), linux_path(folder), str(seed)]
    if ON_WINDOWS:
        command, flags = ["wsl.exe", "-e"] + arguments, subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        command, flags = arguments, 0
    try:
        # Detached from this server's pipes: stdout belongs to the protocol, and the job must outlive a restart.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=flags, start_new_session=not ON_WINDOWS)
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise RuntimeError("Could not start UniRig (%s). On Windows this needs WSL; on Linux it needs bash." % exc) from exc
    state = {"id": job_id, "status": "running", "stage": None, "name": source.stem, "seed": seed, "source": str(source),
             "pid": process.pid, "created": time.time()}
    _write_state(state)
    return _describe(state)


def get_rig(job_id, wait_seconds=45, save_to=None):
    deadline = time.time() + max(0, min(int(wait_seconds), MAX_WAIT))
    while True:
        state = _refresh(_read_state(job_id))
        if state["status"] != "running" or time.time() >= deadline:
            break
        time.sleep(2)
    out = _describe(state)
    if save_to and state["status"] == "done":
        folder = inside_allowed(save_to)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / ("%s_rigged.glb" % state["name"])
        shutil.copyfile(out["files"]["rigged_glb"], target)
        record = folder / ("%s_rigged.provenance.json" % state["name"])
        record.write_text(json.dumps({"rigger": "VAST-AI-Research/UniRig", "seed": state["seed"],
                                      "source_model": state["source"], "bone_count": out.get("bone_count")}, indent=2),
                          encoding="utf-8")
        out["saved"] = {"rigged_glb": str(target), "provenance_json": str(record)}
    return out


def list_rigs(limit=10):
    return [_describe(_refresh(job)) for job in _all_jobs()[:max(1, min(int(limit), 20))]]


def list_bones(model):
    path = model_file(model)
    if path.suffix.lower() != ".glb":
        raise ValueError("list_bones reads GLB files only; %s is %s." % (path.name, path.suffix))
    bones = read_bones(path)
    return {"model": str(path), "bone_count": len(bones), "bones": bones}


def build_server():
    # Imported here so the functions above can be used, and tested, without the MCP SDK.
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    def told(call):
        # The SDK hides the text of any other exception behind "Error executing
        # tool", which leaves the agent with nothing to act on. These are all
        # reasons it can act on: a bad path, a busy GPU, a missing install.
        try:
            return call()
        except (ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc

    server = MCPServer(
        "unirig",
        instructions=(
            "Adds a skeleton and skin weights to a 3D model on this machine's GPU, using UniRig. "
            "Call start_rig, then get_rig with the job id until status is 'done' or 'failed'. "
            "One model is rigged at a time; a second start while one is running is refused, so wait and retry. "
            "A small model takes about a minute and a half. UniRig does not animate: it returns a posable model."
        ),
    )

    @server.tool(name="start_rig")
    def start(model: str, seed: int | None = None) -> dict:
        """Start rigging one 3D model: predict its skeleton, weight its skin, and merge both onto the original.

        model: absolute path on this machine to a GLB, glTF, FBX, OBJ or VRM file. It must be under a folder
            named in UNIRIG_MCP_ROOTS. One whole character or creature works best; the texture is kept.
        seed: leave it out for the default, 12345. The skeleton is sampled, so the same model with another seed
            can come back with a different bone layout. Try another seed when a limb is missing or misplaced.

        Returns the job, including its job_id. The rig is not ready yet: call get_rig with that id.
        """
        return told(lambda: start_rig(model, seed))

    @server.tool(name="get_rig")
    def get(job_id: str, wait_seconds: int = 45, save_to: str | None = None) -> dict:
        """Check on a rigging job, waiting up to wait_seconds (50 at most) for it to finish.

        While status is 'running', 'stage' says where it is ('skeleton', 'skin' or 'merge'); call again. When
        status is 'done', 'files' holds absolute paths to the rigged GLB, the two intermediate FBX files and
        the log, and 'bones' lists every bone with its parent. Bones are named bone_0, bone_1, ...; work out
        which is which from the parent links. When status is 'failed', 'error' holds the end of UniRig's log.

        save_to: optional folder under an allowed root. When the job is done, the rigged GLB is copied there
            as <name>_rigged.glb with a small provenance file beside it, and 'saved' gives their paths.
        """
        return told(lambda: get_rig(job_id, wait_seconds, save_to))

    @server.tool(name="list_rigs")
    def recent(limit: int = 10) -> list[dict]:
        """The most recent rigging jobs, newest first, with their files when finished."""
        return told(lambda: list_rigs(limit))

    @server.tool(name="list_bones")
    def bones(model: str) -> dict:
        """List the bones in any GLB under an allowed folder, each with its parent. Empty for an unrigged model."""
        return told(lambda: list_bones(model))

    return server


if __name__ == "__main__":
    try:
        build_server().run("stdio")
    except Exception as exc:   # anything said here must go to stderr, never stdout
        print("unirig MCP server could not start: %s" % exc, file=sys.stderr)
        raise
