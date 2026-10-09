"""Runs in the operator's separate parser environment (torch + ultralytics).
Prints one JSON line: {"boxes": [{"box": [x, y, x2, y2], "conf": c}], "ms": t, "device": d}.
Used for OmniParser icon_detect weights (ultralytics YOLO format)."""
from __future__ import annotations

import argparse
import json
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--imgsz", type=int, default=1280)
    args = ap.parse_args()
    try:
        from ultralytics import YOLO
    except Exception as exc:
        print(json.dumps({"error": f"ultralytics not importable: {exc}"})); return 2
    device = args.device
    if device == "auto":
        try:
            import torch
            device = "0" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"
    model = YOLO(args.model)
    t0 = time.perf_counter()
    res = model.predict(args.image, imgsz=args.imgsz, conf=args.conf, device=device, verbose=False)
    ms = (time.perf_counter() - t0) * 1000
    boxes = []
    for r in res:
        for b in r.boxes:
            x, y, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
            boxes.append({"box": [x, y, x2, y2], "conf": float(b.conf[0])})
    print(json.dumps({"boxes": boxes, "ms": round(ms, 1), "device": str(device)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
