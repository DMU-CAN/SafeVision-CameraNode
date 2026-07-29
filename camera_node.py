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
import re
from glob import glob
from pathlib import Path
import shlex
import subprocess
import sys
import time

RESTART_BACKOFF_SECONDS = 3


def ffmpeg_binary() -> str:
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    root = Path(__file__).resolve().parent
    configured = os.environ.get("FFMPEG_PATH")
    if configured and Path(configured).exists():
        return configured
    # The ARM64 build in bin/ is required by the Windows ARM camera driver.
    # The x64 distribution under ffmpeg/ is kept as an explicit alternative.
    candidates = (root / "bin" / executable, root / "ffmpeg" / "bin" / executable)
    for bundled in candidates:
        if bundled.exists():
            return str(bundled)
    return "ffmpeg"


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_command(device: str | None = None, port_offset: int = 0, source_override: str | None = None) -> tuple[list[str] | None, list[str]]:
    """Returns (capture_command_or_None, ffmpeg_command). When capture_command
    is set, its stdout must be piped into ffmpeg's stdin (used for CSI)."""

    source = (source_override or env("CAMERA_SOURCE", "auto")).lower()
    if source in {"auto", "windows_auto"} and os.name == "nt":
        source = "windows"
    width = env("WIDTH", "1280")
    height = env("HEIGHT", "720")
    framerate = env("FRAMERATE", "30")
    rtsp_port = str(int(env("RTSP_PORT", "8554")) + port_offset)
    rtsp_path = env("RTSP_PATH", "cam")
    rtsp_host = env("RTSP_HOST", "127.0.0.1" if os.name == "nt" else "0.0.0.0")
    rtsp_url = f"rtsp://{rtsp_host}:{rtsp_port}/{rtsp_path}"

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
            "-f", "rtsp", "-rtsp_flags", "listen",
            rtsp_url,
        ]
        return capture_cmd, ffmpeg_cmd

    if source == "usb":
        device = device or env("CAMERA_DEVICE", "/dev/video0")
        bitrate = env("BITRATE", "2M")
        ffmpeg_cmd = [
            ffmpeg_binary(), "-loglevel", "warning",
            "-f", "v4l2", "-framerate", framerate, "-video_size", f"{width}x{height}",
            "-i", device,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-b:v", bitrate, "-g", framerate,
            "-f", "rtsp", "-rtsp_flags", "listen",
            rtsp_url,
        ]
        return None, ffmpeg_cmd

    if source == "windows":
        # Development-only Windows laptop webcam mode via DirectShow. Keep
        # capture and RTSP publishing in separate FFmpeg processes because
        # some Windows camera drivers cannot be opened in a combined command.
        device = device or env("CAMERA_DEVICE", "Integrated Camera")
        bitrate = env("BITRATE", "2M")
        udp_port = str(10000 + int(env("RTSP_PORT", "8554")) + port_offset)
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
            "-f", "rtsp", "-rtsp_flags", "listen", rtsp_url,
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
            capture_cmd, ffmpeg_cmd = build_command(device=device, port_offset=index, source_override=source)
            print(f"[camera-node] camera {index + 1}: {device}", flush=True)
            print(f"[camera-node] ffmpeg: {shlex.join(ffmpeg_cmd)}", flush=True)
            # Start the RTSP listener first, then attach the camera encoder to
            # its stdin. This avoids a Windows pipe race during startup.
            ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)
            capture_stdout = subprocess.DEVNULL if source == "windows" else ffmpeg_proc.stdin
            capture_proc = subprocess.Popen(capture_cmd or [], stdout=capture_stdout)
            if ffmpeg_proc.stdin:
                ffmpeg_proc.stdin.close()
            processes.extend([capture_proc, ffmpeg_proc])
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
