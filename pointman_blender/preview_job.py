"""Runs inside Blender (`Blender -b <file> --python preview_job.py -- --out DIR`): a copy of the
Mac node's src/messageboard/agent/blender_job.py, for the Blender extension's blender.preview.

Renders a preview still and exports a USDZ the iPhone can open (and show in AR).
Never saves the .blend. For other 3D formats pass `--import PATH` and Blender
starts from an empty scene and imports it.
"""

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--out", required=True)
ap.add_argument("--import", dest="import_path")
ap.add_argument("--width", type=int, default=1280)
ap.add_argument("--height", type=int, default=720)
ap.add_argument("--no-usdz", action="store_true")
ap.add_argument("--max-usdz-mb", type=float, default=250)
args = ap.parse_args(argv)
out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)
info = {"blender": bpy.app.version_string, "warnings": []}


def import_model(path: Path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ext = path.suffix.lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif ext in (".usd", ".usda", ".usdc", ".usdz"):
        bpy.ops.wm.usd_import(filepath=str(path))
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(path))
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=str(path))
    else:
        raise SystemExit(f"can't import {ext}")


if args.import_path:
    import_model(Path(args.import_path))

scene = bpy.context.scene
visible = [o for o in scene.objects if o.visible_get()]
meshes = [o for o in visible if o.type == "MESH"]
info["objects"] = len(scene.objects)
info["meshes"] = len(meshes)
info["polygons"] = sum(len(o.data.polygons) for o in meshes)
info["materials"] = len(bpy.data.materials)
info["frame_range"] = [scene.frame_start, scene.frame_end]


def bounds(objs):
    pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    if not pts:
        return Vector((0, 0, 0)), 1.0
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return (lo + hi) / 2, max((hi - lo).length / 2, 0.01)


if not args.no_usdz:
    usdz = out / "model.usdz"
    # Y-up like SceneKit and AR Quick Look expect; textures capped for the phone.
    # Options vary between Blender versions, so pass only the ones this one has.
    wanted = {"export_materials": True, "generate_preview_surface": True, "convert_orientation": True,
              "export_global_forward_selection": "NEGATIVE_Z", "export_global_up_selection": "Y",
              "export_textures_mode": "NEW", "usdz_downscale_size": "2048"}
    known = {p.identifier for p in bpy.ops.wm.usd_export.get_rna_type().properties}
    try:
        bpy.ops.wm.usd_export(filepath=str(usdz), **{k: v for k, v in wanted.items() if k in known})
    except Exception as e:  # noqa: BLE001 - report and carry on with the preview alone
        info["warnings"].append(f"usdz export failed: {e}")
    if usdz.exists() and usdz.stat().st_size > args.max_usdz_mb * 1024 * 1024:
        info["warnings"].append(f"usdz is {usdz.stat().st_size / 1e6:.0f} MB; skipped")
        usdz.unlink()

if scene.camera is None:
    # frame everything from a three-quarter view
    center, radius = bounds(meshes or visible)
    cam_data = bpy.data.cameras.new("dailies-preview")
    cam = bpy.data.objects.new("dailies-preview", cam_data)
    scene.collection.objects.link(cam)
    direction = Vector((1.0, -1.2, 0.7)).normalized()
    fov = cam_data.angle
    dist = radius / math.sin(fov / 2) * 1.05
    cam.location = center + direction * dist
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam_data.clip_end = max(cam_data.clip_end, dist * 4)
    scene.camera = cam
    info["camera"] = "added"
else:
    info["camera"] = scene.camera.name

if not any(o.type == "LIGHT" for o in visible):
    sun = bpy.data.objects.new("dailies-sun", bpy.data.lights.new("dailies-sun", "SUN"))
    sun.data.energy = 3.0
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    scene.collection.objects.link(sun)
    if scene.world is None:
        scene.world = bpy.data.worlds.new("dailies-world")
    info["light"] = "added"

engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
for engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
    if engine in engines:
        scene.render.engine = engine
        break
info["engine"] = scene.render.engine
try:
    scene.eevee.taa_render_samples = 32
except AttributeError:
    pass
scene.render.resolution_x, scene.render.resolution_y = args.width, args.height
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = str(out / "preview.png")
scene.render.film_transparent = False
bpy.ops.render.render(write_still=True)

(out / "info.json").write_text(json.dumps(info), encoding="utf-8")
