"""The Blender extension's process: `python -m pointman_blender`, started and spoken to by the node.

- blender.render: a .blend's frames in the background, keeping frames already rendered, with
  progress (frames, seconds a frame, ETA, device) and checkpoints, so it resumes after a stop.
- blender.preview: a 3D file's preview still and USDZ, as the Mac's node makes them today.
- blender.status: Blender's version, its GPUs, and what else is using them (Resolve slowed the
  loft's renders from ~1 to ~2 min a frame: scene, thread 868 [1821]).
"""

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

try:
    from pointman_extension import Cancelled, Extension, Retry
except ImportError:                 # a node from before the rename (thread 2255 [3561])
    from mb_extension import Cancelled, Extension, Retry  # type: ignore[no-redef]

ext = Extension()
HERE = Path(__file__).resolve().parent
CANDIDATES = {
    "darwin": ["/Applications/Blender.app/Contents/MacOS/Blender"],
    "linux": ["/run/current-system/sw/bin/blender", "/usr/bin/blender", "/opt/blender/blender"],
    "win32": [r"C:\Program Files\Blender Foundation\Blender\blender.exe"],
}
probed: dict = {}            # version and GPUs, read once from Blender itself
rendering: dict = {}         # the render in progress, for the status


def blender_path() -> str | None:
    given = ext.settings.get("blender_path")
    if given:
        return given if Path(given).exists() else None
    for c in CANDIDATES.get(sys.platform, []):
        if Path(c).exists():
            return c
    return shutil.which("blender")


PROBE = """
import bpy, json
out = {"version": bpy.app.version_string, "devices": []}
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for kind in ("METAL", "OPTIX", "CUDA", "HIP", "ONEAPI"):
        try:
            prefs.compute_device_type = kind
        except TypeError:
            continue
        prefs.get_devices()
        out["devices"] += [{"type": d.type, "name": d.name} for d in prefs.devices if d.type == kind]
except Exception as e:
    out["error"] = str(e)
print("MB " + json.dumps(out), flush=True)
"""


def probe() -> None:
    exe = blender_path()
    if not exe:
        return
    try:
        r = subprocess.run([exe, "-b", "--factory-startup", "--python-expr", PROBE], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("MB ")), None)
        probed.update(json.loads(line[3:]) if line else {"error": (r.stderr or r.stdout)[-300:]})
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        probed["error"] = str(e)
    probed["path"] = exe


def gpu_users() -> list[dict]:
    """Other programs on this machine's GPU: Resolve, other Blender renders, CUDA processes."""
    users = []
    mine = rendering.get("pid")
    if sys.platform == "darwin":
        if subprocess.run(["pgrep", "-x", "Resolve"], capture_output=True).returncode == 0:
            users.append({"name": "DaVinci Resolve"})
        r = subprocess.run(["pgrep", "-f", "Blender.*-b "], capture_output=True, text=True)
        others = [int(p) for p in r.stdout.split() if p.strip().isdigit() and int(p) != mine]
        if others:
            users.append({"name": "Blender", "count": len(others)})
    elif shutil.which("nvidia-smi"):
        r = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True)
        for row in r.stdout.strip().splitlines():
            pid, name, mem = [x.strip() for x in row.split(",")[:3]]
            if pid.isdigit() and int(pid) != mine:
                users.append({"name": Path(name).name, "pid": int(pid), "memory_mb": int(mem or 0)})
    return users


def poll_status() -> None:
    exe = blender_path()
    if not exe:
        ext.event("blender.status", {"installed": False, "reason": "Blender isn't installed here"})
        return
    if probed.get("path") != exe:
        probe()
    ext.event("blender.status", {
        "installed": True, "path": exe, "version": probed.get("version"), "devices": probed.get("devices", []),
        "device": ext.settings.get("device", "auto"), "gpu_users": gpu_users(),
        "rendering": {k: v for k, v in rendering.items() if k != "pid"} or None})


# ---------------------------------------------------------------- rendering

def _clear_placeholder(path: str | None) -> None:
    """A frame this render was in the middle of: Blender left an empty file claiming it, which
    would make every render (here or elsewhere) skip it for good."""
    if path:
        p = Path(path)
        try:
            if p.exists() and p.stat().st_size == 0:
                p.unlink()
        except OSError:
            pass


def _stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(10)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()


@ext.command("blender.render")
def render(job):
    exe = blender_path()
    if not exe:
        raise Retry("Blender isn't installed here")
    p = job.params
    blend = ext.allowed(p["file"])
    if not blend.is_file():
        raise RuntimeError(f"there's no file at {blend}")
    cp = job.checkpoint or {}
    _clear_placeholder(cp.get("current_path"))
    # -Y: a .blend's own scripts never run, whatever Blender's preferences say
    argv = [exe, "-b", "-Y", str(blend)]
    if p.get("scene"):
        argv += ["-S", p["scene"]]
    roots = [*(ext.node.get("roots") or []), ext.node.get("work_dir"), ext.node.get("state_dir")]
    argv += ["--python", str(HERE / "render_job.py"), "--", "--frames", str(p.get("frames") or ""),
             "--device", ext.settings.get("device", "auto"), "--roots", json.dumps([r for r in roots if r])]
    if p.get("output"):
        argv += ["--out", str(ext.allowed(p["output"]))]
    if p.get("camera"):
        argv += ["--camera", p["camera"]]
    if p.get("samples"):
        argv += ["--samples", str(int(p["samples"]))]

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", start_new_session=True)
    lines: queue.Queue = queue.Queue()

    def read() -> None:
        for ln in proc.stdout:
            lines.put(ln)
        lines.put(None)
    threading.Thread(target=read, daemon=True).start()
    tail: deque[str] = deque(maxlen=20)
    started = time.monotonic()
    frames: list[int] = []
    paths: dict[int, str] = {}
    kept = 0
    done_now: list[int] = []
    took: deque[float] = deque(maxlen=8)
    current = {"frame": None, "path": None, "at": time.monotonic()}
    info: dict = {}
    rendering.update(pid=proc.pid, file=blend.name, id=job.id)

    def report() -> None:
        total = len(frames) or 1
        done = kept + len(done_now)
        per = sum(took) / len(took) if took else None
        rendering.update(done=done, total=len(frames))
        job.progress(done / total, "render", frames_done=done, frames_total=len(frames),
                     seconds_per_frame=round(per, 1) if per else None,
                     eta_seconds=round(per * (len(frames) - done)) if per else None,
                     frame=current["frame"], device=info.get("device"))

    there_before: set[int] = set()     # frames on disk, or claimed, before this render began
    wall_start = time.time()

    def ours_unfinished() -> str | None:
        """The frame Blender had claimed when it stopped. It writes the empty claim just before it
        says which frame it's on, so it may be the next one due, if that claim is newer than us."""
        if current["path"]:
            return current["path"]
        for f in frames:
            if f in there_before or f in done_now:
                continue
            try:
                st = os.stat(paths[f])
            except OSError:
                return None
            return paths[f] if st.st_size == 0 and st.st_mtime >= wall_start - 1 else None
        return None

    def handle(line: str) -> None:
        nonlocal info, frames, paths, kept
        if not line.startswith("MB "):
            if line.strip():
                tail.append(line.rstrip())
            return
        msg = json.loads(line[3:])
        ev = msg.pop("event")
        if ev == "start":
            info = msg
            frames = msg["frames"]
            paths = {int(k): v for k, v in msg["paths"].items()}
            kept = sum(1 for f in frames if _rendered(paths[f]))
            there_before.update(f for f in frames if os.path.exists(paths[f]))
            report()
        elif ev == "frame_start":
            current.update(frame=msg["frame"], path=paths.get(msg["frame"]), at=time.monotonic())
            job.save({"current_path": current["path"], "frames_done": kept + len(done_now)})
        elif ev == "frame_done":
            took.append(time.monotonic() - current["at"])
            done_now.append(msg["frame"])
            current.update(frame=None, path=None)
            report()
        elif ev == "error":
            raise RuntimeError(msg.get("message") or "Blender failed")

    try:
        while True:
            if job.cancelled:
                _stop(proc)
                for line in iter(lines.get, None):     # what it said before it stopped: the frame it was on
                    if line.startswith("MB "):
                        handle(line)
                _clear_placeholder(ours_unfinished())
                raise Cancelled("cancelled")
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                break
            handle(line)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"Blender exited with {proc.returncode}: " + " | ".join(list(tail)[-6:]))
    finally:
        if proc.poll() is None:
            _stop(proc)
        rendering.clear()

    on_disk = [f for f in frames if _rendered(paths[f])]
    claimed = [f for f in frames if f not in on_disk and Path(paths[f]).exists()]
    folder = Path(paths[frames[0]]).parent if frames else None
    if folder and on_disk:
        job.output(folder, kind="sequence", meta={
            "frames": on_disk, "first": on_disk[0], "last": on_disk[-1], "file": blend.name,
            "first_file": Path(paths[on_disk[0]]).name,
            "engine": info.get("engine"), "device": info.get("device"), "resolution": info.get("resolution")})
    return {"frames": len(frames), "rendered": len(done_now), "kept": kept, "claimed_elsewhere": claimed,
            "missing": [f for f in frames if f not in on_disk and f not in claimed],
            "seconds": round(time.monotonic() - started, 1), "device": info.get("device"),
            "folder": str(folder) if folder else None}


def _rendered(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


# ---------------------------------------------------------------- previews

@ext.command("blender.preview")
def preview(job):
    exe = blender_path()
    if not exe:
        raise Retry("Blender isn't installed here")
    src = ext.allowed(job.params["file"])
    if not src.is_file():
        raise RuntimeError(f"there's no file at {src}")
    out = job.work / "blender"
    ext_ = src.suffix.lower()
    cmd = [exe, "-b", "-Y", "--factory-startup"]
    cmd += [str(src)] if ext_ == ".blend" else []
    cmd += ["--python", str(HERE / "preview_job.py"), "--", "--out", str(out)]
    if ext_ != ".blend":
        cmd += ["--import", str(src)]
    if ext_ == ".usdz":
        cmd.append("--no-usdz")
    log_path = job.work / "blender.log"
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
    job.progress(None, "blender")
    while proc.poll() is None:
        if job.cancelled:
            _stop(proc)
            raise Cancelled("cancelled")
        time.sleep(0.25)
    log = log_path.read_text(encoding="utf-8", errors="replace")
    info_path = out / "info.json"
    if proc.returncode != 0 or not info_path.exists():
        raise RuntimeError("Blender failed: " + " | ".join(log.strip().splitlines()[-8:]))
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info.update(bytes=src.stat().st_size, format=ext_.lstrip("."))
    usdz = src if ext_ == ".usdz" else out / "model.usdz"
    if usdz.exists():
        job.output(usdz, kind="model", meta=info)
    if (out / "preview.png").exists():
        job.output(out / "preview.png", kind="image", role="poster" if usdz.exists() else "main", meta=info)
    if not usdz.exists() and not (out / "preview.png").exists():
        raise RuntimeError("Blender produced nothing to show")
    return info


ext.every(15, poll_status, lane="status")

if __name__ == "__main__":
    ext.run()
