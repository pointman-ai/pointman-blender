"""Runs inside Blender: `Blender -b file.blend [-S scene] --python render_job.py -- --frames 1-250 ...`.

Renders the frames asked for, keeping any already there: Blender skips a frame whose file exists, and
claims the one it's rendering with an empty placeholder, so a stopped render picks up where it left
off and two machines can share one output folder (scene, thread 868 [1821]). Never saves the .blend.

Speaks to the extension in lines starting `MB ` on stdout, each a JSON object.
"""

import argparse
import json
import sys
from pathlib import Path

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--frames", default="")
ap.add_argument("--out", default="")
ap.add_argument("--camera", default="")
ap.add_argument("--samples", type=int, default=0)
ap.add_argument("--device", default="auto")
ap.add_argument("--roots", default="[]", help="JSON list: folders frames may be written in")
args = ap.parse_args(argv)


def say(event: str, **data) -> None:
    print("MB " + json.dumps({"event": event, **data}), flush=True)


def frames_from(spec: str, scene) -> list[int]:
    if not spec.strip():
        return list(range(scene.frame_start, scene.frame_end + 1, max(1, scene.frame_step)))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out += range(int(a), int(b) + 1)
        elif part:
            out.append(int(part))
    return sorted(set(out))


def use_device(scene, want: str) -> str:
    """Cycles' device lives in Blender's preferences, not the .blend, so it's set on every render."""
    if scene.render.engine != "CYCLES":
        return scene.render.engine
    if want == "CPU":
        scene.cycles.device = "CPU"
        return "CPU"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for kind in ([want] if want != "auto" else ["METAL", "OPTIX", "CUDA", "HIP", "ONEAPI"]):
        try:
            prefs.compute_device_type = kind
        except TypeError:          # not a backend this build of Blender has
            continue
        prefs.get_devices()
        if any(d.type == kind for d in prefs.devices):
            for d in prefs.devices:
                d.use = d.type == kind
            scene.cycles.device = "GPU"
            return kind
    scene.cycles.device = "CPU"
    return "CPU"


scene = bpy.context.scene
if args.camera:
    cam = bpy.data.objects.get(args.camera)
    if cam is None or cam.type != "CAMERA":
        say("error", message=f"no camera called {args.camera!r}")
        sys.exit(2)
    scene.camera = cam
if args.out:
    Path(args.out).mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(Path(args.out) / "####")
if args.samples:
    if scene.render.engine == "CYCLES":
        scene.cycles.samples = args.samples
    elif hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = args.samples
device = use_device(scene, args.device)
scene.render.use_overwrite = False
scene.render.use_placeholder = True

frames = frames_from(args.frames, scene)
paths = {f: scene.render.frame_path(frame=f) for f in frames}
roots = [Path(r).resolve() for r in json.loads(args.roots)]
if roots:
    folder = Path(paths[frames[0]]).resolve().parent if frames else None
    if folder is None or not any(folder == r or r in folder.parents for r in roots):
        say("error", message=f"the .blend renders to {folder}, outside the folders this node allows; "
                             "give an output folder inside them")
        sys.exit(3)
say("start", blender=bpy.app.version_string, engine=scene.render.engine, device=device, scene=scene.name,
    frames=frames, paths={str(f): p for f, p in paths.items()},
    resolution=[scene.render.resolution_x * scene.render.resolution_percentage // 100,
                scene.render.resolution_y * scene.render.resolution_percentage // 100])


def on_pre(scn, *_):
    say("frame_start", frame=scn.frame_current)


def on_write(scn, *_):
    say("frame_done", frame=scn.frame_current, path=scn.render.frame_path(frame=scn.frame_current))


bpy.app.handlers.render_pre.append(on_pre)
bpy.app.handlers.render_write.append(on_write)

# contiguous runs of frames, each one animation render: Blender skips the frames already on disk
runs: list[list[int]] = []
for f in frames:
    if runs and f == runs[-1][-1] + 1:
        runs[-1].append(f)
    else:
        runs.append([f])
step = scene.frame_step
scene.frame_step = 1
for run in runs:
    scene.frame_start, scene.frame_end = run[0], run[-1]
    bpy.ops.render.render(animation=True)
scene.frame_step = step
say("finished")
