# blender

A Pointman extension for Blender on a node. It makes previews of 3D files: a still and a USDZ, which opens
in 3D and AR on an iPhone or iPad. It also renders a .blend's frames in the background on the machine's
GPU. A render keeps the frames already on disk, so it resumes after a stop, and its progress shows live on
the board: frames done, seconds a frame, time left and the device in use.

Python (standard library only), run by the node on Python 3.12. Blender's own Python runs the two
scripts in `pointman_blender/` that do the work inside Blender.

```
pointman add blender
```

## What it needs

- **Blender**, found by itself at `/Applications/Blender.app` on macOS, `/usr/bin/blender`,
  `/opt/blender/blender` or NixOS's system path on Linux, `C:\Program Files\Blender Foundation` on Windows,
  or on the PATH. The `blender_path` setting points it at another install.
- A GPU for fast renders: Metal, OptiX, CUDA, HIP or oneAPI, whichever Blender's Cycles finds. Without
  one it renders on the CPU.

A .blend's own scripts never run (`-Y`), whatever Blender's preferences say, and the .blend is never saved.

## Commands

Agents get each as a tool (`blender_preview`, `blender_render`), or call it on a machine with
`run_on_machine`.

| Kind | Params | What |
|---|---|---|
| `blender.preview` | `file`, `thread_id?` | A preview still (1280×720) and a USDZ of a .blend, .glb, .gltf, .fbx, .obj or .usd/.usda/.usdc/.usdz file. Cancellable |
| `blender.render` | `file`, `frames?`, `scene?`, `camera?`, `samples?`, `output?`, `thread_id?` | Renders a .blend's frames in the background, one render at a time per machine (lane `gpu`). Cancellable and resumable |

- **`frames`** is `1-250` or `1,5,10-20`. Left out, it's the scene's range.
- **`output`** is a folder on the machine. Left out, frames go where the .blend says. Either way it must be
  in a folder the node allows.
- **Resuming:** Blender skips a frame whose file is already there and claims the one it's rendering with an
  empty placeholder. So a stopped render picks up where it left off, two machines can share one output
  folder, and a stop clears the claim on the frame it was in the middle of.
- **The result:** the folder of frames as a sequence, and how many were rendered, kept, claimed by another
  machine, or missing.
- **Across machines:** asked with `split`, one render is shared by every machine running Blender, each
  taking runs of frames as it's free, and the parts are joined into one video at the end.

On a Mac a .blend gets "Open in Blender", and a 3D file in any of the formats above is shown through
`blender.preview`.

## Status

`blender.status`, every 15 s: whether Blender is installed, its path and version, the GPUs Cycles sees, the
device in use, the render under way, and what else is using the GPU (DaVinci Resolve, another Blender
render, or CUDA processes on Linux).

## Settings

Per machine, in `~/.config/pointman/extensions/blender/config.toml`, or
`pointman config set node.extensions.blender.<key> <value>`:

| Setting | Default | |
|---|---|---|
| `blender_path` | found by itself | The Blender to run |
| `device` | `auto` | The render device: `auto`, `METAL`, `OPTIX`, `CUDA`, `HIP`, `ONEAPI` or `CPU`. Set on every render, since Blender keeps it in its preferences, not the .blend |
