# Bundled model assets

| File | Purpose | Source | License | SHA-256 |
|------|---------|--------|---------|---------|
| `face_detection_yunet_2023mar.onnx` | CPU face detection (YuNet) for the presenter-region detector | https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet | Apache-2.0 (OpenCV Zoo) | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |

The detector verifies the checksum before loading. YuNet produces face boxes and five
landmarks; the monitor uses them only for presence/motion within the configured presenter
region. No face embeddings or identity recognition are computed or stored.

Re-download: `curl -L -o models/face_detection_yunet_2023mar.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx`
