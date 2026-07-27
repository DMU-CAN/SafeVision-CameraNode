"""SafeVision camera node: captures video from a CSI camera (rpicam-vid) or a
USB webcam (v4l2) and serves it as an RTSP stream that ffmpeg hosts itself
(no separate RTSP server binary needed, via `-rtsp_flags listen`).

The main SafeVision backend connects directly to this node's RTSP URL
(rtsp://<node-ip>:<port>/<path>) — see SafeVision-Backend's camera
registration (`POST /api/v1/cameras`).

Configuration is read from environment variables (see .env.example):
    CAMERA_SOURCE       "csi" or "usb"          (default: usb)
    CAMERA_DEVICE       v4l2 device path         (default: /dev/video0, usb only)
    WIDTH, HEIGHT       capture resolution       (default: 1280x720)
    FRAMERATE           capture fps              (default: 30)
    BITRATE             encoder bitrate, USB only (default: 2M)
    RTSP_PORT           port to listen on        (default: 8554)
    RTSP_PATH           stream path              (default: cam)
"""

import os
import shlex
import subprocess
import sys
import time

RESTART_BACKOFF_SECONDS = 3


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_command() -> tuple[list[str] | None, list[str]]:
    """Returns (capture_command_or_None, ffmpeg_command). When capture_command
    is set, its stdout must be piped into ffmpeg's stdin (used for CSI)."""

    source = env("CAMERA_SOURCE", "usb").lower()
    width = env("WIDTH", "1280")
    height = env("HEIGHT", "720")
    framerate = env("FRAMERATE", "30")
    rtsp_port = env("RTSP_PORT", "8554")
    rtsp_path = env("RTSP_PATH", "cam")
    rtsp_url = f"rtsp://0.0.0.0:{rtsp_port}/{rtsp_path}"

    if source == "csi":
        # rpicam-vid hardware-encodes H264 directly on the Pi's camera ISP —
        # ffmpeg just remuxes it into RTSP without re-encoding (-c copy).
        capture_cmd = [
            "rpicam-vid",
            "--codec", "h264",
            "-o", "-",
            "-t", "0",
            "--width", width,
            "--height", height,
            "--framerate", framerate,
            "--nopreview",
        ]
        ffmpeg_cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-f", "h264", "-i", "-",
            "-c:v", "copy",
            "-f", "rtsp", "-rtsp_flags", "listen",
            rtsp_url,
        ]
        return capture_cmd, ffmpeg_cmd

    if source == "usb":
        device = env("CAMERA_DEVICE", "/dev/video0")
        bitrate = env("BITRATE", "2M")
        ffmpeg_cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-f", "v4l2", "-framerate", framerate, "-video_size", f"{width}x{height}",
            "-i", device,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-b:v", bitrate, "-g", framerate,
            "-f", "rtsp", "-rtsp_flags", "listen",
            rtsp_url,
        ]
        return None, ffmpeg_cmd

    raise ValueError(f"Unknown CAMERA_SOURCE: {source!r} (expected 'csi' or 'usb')")


def run_once() -> int:
    capture_cmd, ffmpeg_cmd = build_command()
    print(f"[camera-node] ffmpeg: {shlex.join(ffmpeg_cmd)}", flush=True)

    if capture_cmd is None:
        return subprocess.run(ffmpeg_cmd).returncode

    print(f"[camera-node] capture: {shlex.join(capture_cmd)}", flush=True)
    capture_proc = subprocess.Popen(capture_cmd, stdout=subprocess.PIPE)
    try:
        ffmpeg_proc = subprocess.run(ffmpeg_cmd, stdin=capture_proc.stdout)
        return ffmpeg_proc.returncode
    finally:
        capture_proc.terminate()
        capture_proc.wait(timeout=5)


def main() -> None:
    while True:
        try:
            exit_code = run_once()
            print(f"[camera-node] stream exited with code {exit_code}, restarting in {RESTART_BACKOFF_SECONDS}s", flush=True)
        except FileNotFoundError as exc:
            print(f"[camera-node] required binary not found: {exc}", file=sys.stderr, flush=True)
        except ValueError as exc:
            print(f"[camera-node] config error: {exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        time.sleep(RESTART_BACKOFF_SECONDS)


if __name__ == "__main__":
    main()
