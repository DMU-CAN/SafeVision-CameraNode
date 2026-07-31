"""SafeVision camera node: captures video from a CSI camera (rpicam-vid) or a
USB webcam (v4l2), and publishes it to a local MediaMTX RTSP server, which is
what actually serves the stream to the main SafeVision backend.

Earlier versions of this script had ffmpeg host the RTSP server itself via
`-rtsp_flags listen`. That mode turned out to be unreliable in practice (it
fails with `Connection refused` on some ffmpeg builds/platforms even for a
plain synthetic test source, unrelated to the camera) — MediaMTX + ffmpeg
push is the verified-working replacement.

The main SafeVision backend connects to this node's RTSP URL
(rtsp://<node-ip>:<port>/<path>) — see SafeVision-Backend's camera
registration (`POST /api/v1/cameras`).

Configuration is read from environment variables (see .env.example):
    CAMERA_SOURCE       "csi" or "usb"          (default: usb)
    CAMERA_DEVICE       v4l2 device path         (default: /dev/video0, usb only)
    WIDTH, HEIGHT       capture resolution       (default: 1280x720)
    FRAMERATE           capture fps              (default: 30)
    BITRATE             encoder bitrate, USB only (default: 2M)
    RTSP_PORT           MediaMTX RTSP port       (default: 8554)
    RTSP_PATH           stream path              (default: cam)

MediaMTX itself needs its binary available — see README.md for where to get
it. Resolution order: $MEDIAMTX_PATH, ./mediamtx/mediamtx(.exe) next to this
script, then whatever `mediamtx` resolves to on PATH.
"""

import os
import re
import threading
from glob import glob
from pathlib import Path
import shlex
import subprocess
import sys
import time

RESTART_BACKOFF_SECONDS = 3
MEDIAMTX_STARTUP_GRACE_SECONDS = 2


def ffmpeg_binary() -> str:
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    root = Path(__file__).resolve().parent
    configured = os.environ.get("FFMPEG_PATH")
    if configured and Path(configured).exists():
        return configured
    # The ARM64 build in bin/ is required by the Windows ARM camera driver.
    # The x64 distribution under ffmpeg/ is kept as an explicit alternative.
    candidates = (root / "ffmpeg" / "bin" / executable, root / "bin" / executable)
    for bundled in candidates:
        if bundled.exists():
            return str(bundled)
    return "ffmpeg"


def mediamtx_binary() -> str:
    executable = "mediamtx.exe" if os.name == "nt" else "mediamtx"
    root = Path(__file__).resolve().parent
    configured = os.environ.get("MEDIAMTX_PATH")
    if configured and Path(configured).exists():
        return configured
    bundled = root / "mediamtx" / executable
    if bundled.exists():
        return str(bundled)
    return executable


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def run_mediamtx_forever() -> None:
    """MediaMTX is the actual RTSP server for this node — it just needs to
    stay up; ffmpeg (run separately, see run_capture_forever) publishes into
    it rather than hosting RTSP itself. Runs on its own restart loop, same
    supervision style as the capture pipeline below."""
    binary = mediamtx_binary()
    root = Path(__file__).resolve().parent
    config = root / "mediamtx.yml"
    command = [binary] + ([str(config)] if config.exists() else [])

    while True:
        print(f"[camera-node] mediamtx: {shlex.join(command)}", flush=True)
        try:
            process = subprocess.Popen(command, cwd=str(root))
            process.wait()
            print(f"[camera-node] mediamtx exited with code {process.returncode}", flush=True)
        except FileNotFoundError:
            print(
                "[camera-node] mediamtx binary not found — see README.md for where to get it "
                "(or set MEDIAMTX_PATH). Camera capture cannot publish anywhere without it.",
                file=sys.stderr, flush=True,
            )
        print(f"[camera-node] restarting mediamtx in {RESTART_BACKOFF_SECONDS}s", flush=True)
        time.sleep(RESTART_BACKOFF_SECONDS)


def publish_target(stream_index: int) -> str:
    """Where ffmpeg pushes to. Defaults to this node's own local MediaMTX
    (127.0.0.1) — started separately, see run_mediamtx_forever — which is
    right when the backend can reach this node's RTSP port directly.

    If the camera and backend are on different networks/firewalls, set
    PUBLISH_HOST (and PUBLISH_USER/PUBLISH_PASS if the target requires auth,
    e.g. the main backend's own MediaMTX, see SafeVision-Backend README
    §7.1) to push straight to that remote server instead — this node then
    needs no inbound port open at all, only outbound to PUBLISH_HOST."""
    rtsp_port = env("RTSP_PORT", "8554")
    rtsp_path = env("RTSP_PATH", "cam")
    if stream_index > 0:
        rtsp_path = f"{rtsp_path}{stream_index + 1}"

    host = env("PUBLISH_HOST", f"127.0.0.1:{rtsp_port}")
    user = os.environ.get("PUBLISH_USER")
    password = os.environ.get("PUBLISH_PASS")
    auth = f"{user}:{password}@" if user else ""
    return f"rtsp://{auth}{host}/{rtsp_path}"


def build_command(device: str | None = None, stream_index: int = 0, source_override: str | None = None) -> tuple[list[str] | None, list[str]]:
    """Returns (capture_command_or_None, ffmpeg_command). When capture_command
    is set, its stdout must be piped into ffmpeg's stdin (used for CSI). The
    ffmpeg command always publishes (pushes) — it never hosts RTSP itself.
    See publish_target() for where it pushes to.

    `stream_index` distinguishes multiple cameras on the same node (see
    run_auto): the second+ camera gets RTSP_PATH suffixed with its 1-based
    index (cam, cam2, cam3, ...) rather than a different port."""

    source = (source_override or env("CAMERA_SOURCE", "auto")).lower()
    if source in {"auto", "windows_auto"} and os.name == "nt":
        source = "windows"
    width = env("WIDTH", "1280")
    height = env("HEIGHT", "720")
    framerate = env("FRAMERATE", "30")
    publish_url = publish_target(stream_index)

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
            ffmpeg_binary(), "-loglevel", "warning",
            "-f", "h264", "-i", "-",
            "-c:v", "copy",
            "-f", "rtsp",
            publish_url,
        ]
        return capture_cmd, ffmpeg_cmd

    if source == "usb":
        device = device or env("CAMERA_DEVICE", "/dev/video0")
        bitrate = env("BITRATE", "2M")
        ffmpeg_cmd = [
            ffmpeg_binary(), "-loglevel", "warning",
            # MJPEG capture instead of raw: raw YUYV at 720p+ commonly
            # saturates USB2 bandwidth and gets throttled to a fraction of
            # the requested framerate by the driver; MJPEG is compressed
            # on-camera so it fits comfortably.
            "-f", "v4l2", "-input_format", "mjpeg", "-framerate", framerate, "-video_size", f"{width}x{height}",
            "-i", device,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-b:v", bitrate, "-g", framerate,
            "-f", "rtsp",
            publish_url,
        ]
        return None, ffmpeg_cmd

    if source == "windows":
        # Development-only Windows laptop webcam mode via DirectShow. Keep
        # capture and RTSP publishing in separate FFmpeg processes because
        # some Windows camera drivers cannot be opened in a combined command.
        device = device or env("CAMERA_DEVICE", "Integrated Camera")
        bitrate = env("BITRATE", "2M")
        udp_port = str(10000 + int(env("RTSP_PORT", "8554")) + stream_index)
        capture_cmd = [
            ffmpeg_binary(), "-loglevel", "warning",
            "-f", "dshow", "-video_size", f"{width}x{height}",
            "-framerate", framerate, "-i", f"video={device}",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-b:v", bitrate, "-g", framerate,
            # MPEG-TS carries codec metadata through the pipe so the RTSP
            # process can initialize even when the camera starts mid-stream.
            "-f", "mpegts", f"udp://127.0.0.1:{udp_port}?pkt_size=1316",
        ]
        ffmpeg_cmd = [
            ffmpeg_binary(), "-loglevel", "warning",
            "-analyzeduration", "2M", "-probesize", "5M",
            "-f", "mpegts", "-i", f"udp://127.0.0.1:{udp_port}?fifo_size=1000000&overrun_nonfatal=1", "-c:v", "copy",
            "-f", "rtsp",
            publish_url,
        ]
        return capture_cmd, ffmpeg_cmd

    raise ValueError(f"Unknown CAMERA_SOURCE: {source!r} (expected 'csi', 'usb', 'windows', or 'windows_auto')")


def list_windows_cameras() -> list[str]:
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    names: list[str] = []
    for line in f"{result.stdout}\n{result.stderr}".splitlines():
        match = re.search(r'"([^"]+)" \((?:none|video|audio)\)', line)
        if match and "alternative name" not in line.lower() and match.group(1) not in names:
            names.append(match.group(1))
    return names


def list_linux_cameras() -> list[str]:
    return sorted(glob("/dev/video*"))


def run_auto() -> None:
    if os.name == "nt":
        devices = list_windows_cameras()
        source = "windows"
    else:
        devices = list_linux_cameras()
        source = "usb"
    if not devices:
        raise RuntimeError("No camera devices found")
    processes: list[subprocess.Popen] = []
    try:
        for index, device in enumerate(devices):
            capture_cmd, ffmpeg_cmd = build_command(device=device, stream_index=index, source_override=source)
            print(f"[camera-node] camera {index + 1}: {device}", flush=True)
            print(f"[camera-node] ffmpeg: {shlex.join(ffmpeg_cmd)}", flush=True)
            capture_proc = subprocess.Popen(capture_cmd, stdout=subprocess.PIPE) if capture_cmd else None
            ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=capture_proc.stdout if capture_proc else None)
            if capture_proc and capture_proc.stdout:
                capture_proc.stdout.close()  # let ffmpeg see EOF if capture_proc dies, not just this reference
                processes.append(capture_proc)
            processes.append(ffmpeg_proc)
        while any(process.poll() is None for process in processes):
            time.sleep(1)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()


def run_once() -> int:
    if env("CAMERA_SOURCE", "auto").lower() in {"auto", "windows_auto"}:
        run_auto()
        return 0
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
    if os.environ.get("PUBLISH_HOST"):
        # Pushing straight to a remote MediaMTX (see publish_target) — this
        # node doesn't need its own RTSP server at all.
        print(f"[camera-node] PUBLISH_HOST set, pushing directly to {os.environ['PUBLISH_HOST']} (no local mediamtx)", flush=True)
    else:
        threading.Thread(target=run_mediamtx_forever, daemon=True).start()
        time.sleep(MEDIAMTX_STARTUP_GRACE_SECONDS)  # give it a moment to bind before ffmpeg tries to publish

    while True:
        try:
            exit_code = run_once()
            print(f"[camera-node] stream exited with code {exit_code}, restarting in {RESTART_BACKOFF_SECONDS}s", flush=True)
        except FileNotFoundError as exc:
            print(f"[camera-node] required binary not found: {exc}", file=sys.stderr, flush=True)
        except (RuntimeError, ValueError) as exc:
            print(f"[camera-node] config error: {exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        time.sleep(RESTART_BACKOFF_SECONDS)


if __name__ == "__main__":
    main()
