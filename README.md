# SafeVision-CameraNode

Runs on each camera node (fixed CCTV Raspberry Pi, or a robot's onboard Pi)
and turns its camera into an RTSP source the main SafeVision backend connects
to. This process does no AI/analysis work — that all happens centrally on the
main Pi's Hailo-8 (see the backend's `HAILO_PORTING.md`). This node only
captures and serves video.

Supports two camera sources:
- **csi** — Raspberry Pi Camera Module via `rpicam-vid` (hardware H264 encode,
  no re-encoding, lowest CPU use). Use this on real deployments.
- **usb** — any USB webcam via `ffmpeg`'s v4l2 input (software H264 encode).
  Also what you'd use for ad-hoc testing.

`ffmpeg` hosts the RTSP stream itself (`-rtsp_flags listen`) — no separate
RTSP server (e.g. mediamtx) needs to run on the camera node.

## Requirements

- `ffmpeg` (with libx264)
- `rpicam-vid` (only needed for `CAMERA_SOURCE=csi`; ships with Raspberry Pi OS /
  most Pi-based Ubuntu images as part of `libcamera-apps`/`rpicam-apps`)
- Python 3.9+

## Setup

```bash
cp .env.example .env
# edit .env: CAMERA_SOURCE, CAMERA_DEVICE (usb), resolution, etc.
python3 camera_node.py
```

The stream will be available at `rtsp://<this-node-ip>:8554/cam` (path/port
configurable via `.env`). Register it with the backend:

```bash
curl -X POST http://<backend-ip>:8080/api/v1/cameras \
  -H "Content-Type: application/json" \
  -d '{"name":"<name>","rtspUrl":"rtsp://<this-node-ip>:8554/cam","location":"<location>"}'
```

## Running as a service (production)

```bash
sudo mkdir -p /opt/safevision-camera-node
sudo cp camera_node.py .env /opt/safevision-camera-node/
sudo cp camera-node.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now camera-node
```

`Restart=always` means it comes back up automatically if `ffmpeg`/`rpicam-vid`
crashes or the camera is briefly unplugged.

## Notes

- Only one client can read the stream at a time as written (`ffmpeg -rtsp_flags
  listen` is a single-connection RTSP server). If multiple viewers need to
  watch the same node simultaneously, put mediamtx in front of it later —
  not needed for the current single-backend-consumer setup.
- For CSI, `ffmpeg` does a stream copy (`-c:v copy`) — no software encoding,
  so resolution/framerate changes take effect on `rpicam-vid`'s side, not
  ffmpeg's.
