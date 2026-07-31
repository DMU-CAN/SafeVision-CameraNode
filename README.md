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

`ffmpeg` captures and encodes, then **publishes (pushes) into a local
MediaMTX instance**, which is the actual RTSP server other things connect to.
(An earlier version had ffmpeg host RTSP itself via `-rtsp_flags listen` —
that turned out to be unreliable in practice, failing with `Connection
refused` even for a synthetic test source on some ffmpeg builds/platforms,
unrelated to the camera. MediaMTX + push is the verified-working setup, and
as a bonus supports multiple simultaneous viewers, which the old single-
connection listen mode didn't.)

## Requirements

- `ffmpeg` (with libx264)
- `rpicam-vid` (only needed for `CAMERA_SOURCE=csi`; ships with Raspberry Pi OS /
  most Pi-based Ubuntu images as part of `libcamera-apps`/`rpicam-apps`)
- **MediaMTX** — download the binary for your platform from
  [bluenviron/mediamtx releases](https://github.com/bluenviron/mediamtx/releases)
  (asset name pattern `mediamtx_<version>_<os>_<arch>.tar.gz`, e.g.
  `mediamtx_v1.19.3_linux_arm64.tar.gz` for a Raspberry Pi). Extract it to
  `mediamtx/mediamtx` (or `mediamtx/mediamtx.exe` on Windows) next to
  `camera_node.py`, or point `MEDIAMTX_PATH` at it, or just have `mediamtx`
  on `PATH`. The repo's `mediamtx.yml` (same directory as `camera_node.py`)
  is picked up automatically — without it MediaMTX starts with an empty
  config and refuses to let ffmpeg publish at all (`path 'cam' is not
  configured`), so don't delete it.
- Python 3.9+

## Setup

```bash
cp .env.example .env
# edit .env: CAMERA_SOURCE, CAMERA_DEVICE (usb), resolution, etc.
python3 camera_node.py
```

`camera_node.py` starts MediaMTX itself (in the background, auto-restarted if
it ever exits) — you don't need to run it separately. The stream will be
available at `rtsp://<this-node-ip>:8554/cam` (path/port configurable via
`.env`). Register it with the backend:

```bash
curl -X POST http://<backend-ip>:8080/api/v1/cameras \
  -H "Content-Type: application/json" \
  -d '{"name":"<name>","rtspUrl":"rtsp://<this-node-ip>:8554/cam","location":"<location>"}'
```

## Running as a service (production)

```bash
sudo mkdir -p /opt/safevision-camera-node
sudo cp -r camera_node.py .env mediamtx.yml mediamtx/ /opt/safevision-camera-node/
sudo cp camera-node.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now camera-node
```

`Restart=always` means it comes back up automatically if `ffmpeg`/`rpicam-vid`
crashes or the camera is briefly unplugged. MediaMTX has its own restart loop
inside `camera_node.py`, independent of the capture pipeline.

## Notes

- For CSI, `ffmpeg` does a stream copy (`-c:v copy`) — no software encoding,
  so resolution/framerate changes take effect on `rpicam-vid`'s side, not
  ffmpeg's.
- For USB, capture uses MJPEG (`-input_format mjpeg`) rather than raw YUYV —
  raw video at 720p+ commonly saturates USB2 bandwidth and gets silently
  throttled to a fraction of the requested framerate by the v4l2 driver;
  MJPEG is compressed on-camera so it fits comfortably (verified: raw YUYV at
  1280x720 capped at ~7.5fps on a Logitech webcam over USB2, MJPEG hit the
  full 30fps at the same resolution).
- Pushing into a remote MediaMTX instead of a local one (e.g. the main
  server's own MediaMTX, for a camera on a different network than the
  backend) is also possible — set `RTSP_PATH`'s target host by publishing
  directly to that server's address instead of running a local MediaMTX; see
  `SafeVision-Backend/README.md` §7.1 for that setup.

# Windows laptop webcam test mode

For local development, set `CAMERA_SOURCE=windows` to capture the laptop
webcam through FFmpeg DirectShow and publish the same RTSP stream as a real
camera node.

List available camera names:

```powershell
ffmpeg -list_devices true -f dshow -i dummy
```

Example `.env`:

```env
CAMERA_SOURCE=windows
CAMERA_DEVICE=Integrated Camera
RTSP_PORT=8554
RTSP_PATH=cam
```

Run with `python camera_node.py`. The stream is available at
`rtsp://<laptop-ip>:8554/cam`. Use `csi` or `usb` on Raspberry Pi deployments.

# Automatic multi-camera mode

`CAMERA_SOURCE=auto` is the default. The node discovers all available camera
inputs and publishes each one to the same MediaMTX instance under its own
path, starting at `RTSP_PATH`:

```text
camera 1 -> rtsp://<node-ip>:8554/cam
camera 2 -> rtsp://<node-ip>:8554/cam2
camera 3 -> rtsp://<node-ip>:8554/cam3
```

On Windows, DirectShow devices are discovered automatically. On Linux/Raspberry
Pi, `/dev/video*` devices are discovered automatically. Set `CAMERA_SOURCE=csi`
or `CAMERA_SOURCE=usb` only when a single explicit source is required.
