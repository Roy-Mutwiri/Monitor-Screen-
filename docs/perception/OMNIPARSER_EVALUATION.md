# OmniParser evaluation (2026-10-09)

**Decision**: not bundled. The evidence-based discovery (accessibility probe + Windows OCR word boxes + visual
anchors) locates every element the monitor needs on the real Studio frame in ~60 ms on CPU, so a 280 MB GPU
detector is not required. OmniParser remains available as an *optional external* refinement
(`perception/omniparser.py`): it runs in the operator's own Python environment, in a separate process with a
timeout, and only adds generic "interactable element" boxes.

## Licence and components (from the repository and the model card)

| Component | Licence | Notes |
|---|---|---|
| Repository code | CC-BY-4.0 | |
| `icon_detect` (v2, YOLOv8 via Ultralytics) | **AGPL-3.0** | loads with `ultralytics`; AGPL makes it unsuitable for bundling in this MIT-style project |
| `icon_detect_v3` (YOLOv9-E, "MultimediaTechLab/YOLO" implementation) | MIT | shipped as a **TorchScript** archive; `ultralytics` cannot load it, so decoding + NMS need the upstream inference code |
| `icon_caption` (Florence-2) | MIT | not needed (no captions required) |

Dependencies for either detector: `torch` (CUDA build ≈ 2.5 GB download), `ultralytics` (v2) or the
MultimediaTechLab YOLO code (v3). Hardware: GPU recommended; CPU works at ~3 fps (v2) / ~1 fps (v3).

## Benchmark on this machine

Hardware: NVIDIA GeForce RTX 5060 Ti (16 GB), 16 CPU threads, Windows 11 build 26200, torch CUDA 12.8 build in a
separate environment (`D:\Reproduced Content\omni-bench\.venv`, not part of the repository).
Input: the operator's real Studio frame (1512×726, private fixture).

| Model | Device | Median latency | Output |
|---|---|---|---|
| `icon_detect` v2 (AGPL) via ultralytics, imgsz 1280, conf 0.3 | GPU | **32 ms** | 61 boxes (buttons, icons, list rows, chat avatar, sliders) |
| `icon_detect` v2 | CPU | **354 ms** | 61 boxes |
| `icon_detect_v3` (MIT) TorchScript, 640×640 forward only | GPU | 31.6 ms | raw heads (1,1,80,80)…(1,4,20,20); decoding/NMS not implemented here |
| `icon_detect_v3` | CPU | 1041 ms | — |
| Evidence-based discovery (this project) | CPU | OCR 67 ms + discovery 51–57 ms | 11 typed elements with confidence and source |

Observations on the 61 v2 boxes: the detector finds generic UI widgets (every Tools tile, list rows, the Go LIVE
button, sliders, the top-bar icons) but carries no semantics; the program preview and the level meter are not
distinguished from other rectangles. It therefore cannot replace the typed discovery, only refine it.

## How to enable the optional backend

1. Create an environment with `torch` and `ultralytics`; download `icon_detect/model.pt` from
   `microsoft/OmniParser-v2.0` (AGPL — for your own use).
2. Settings → Automatic detection: set the parser Python path and model path, enable the parser.
3. The monitor calls `perception/omniparser_runner.py` in that environment on each discovery pass (timeout 20 s);
   results appear as `ui_element` boxes in the layout overlay. Heavy inference never runs inside the GUI/capture
   process.

No training was performed. Nothing here is "trained on Studio screenshots".
