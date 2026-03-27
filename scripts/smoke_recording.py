"""Deterministic recording smoke test for the local runtime.

This script validates the container/runtime recording chain without depending on
external meeting providers. It launches the virtual environment, opens a local
animated page in Chromium, records a short sample via FFmpeg, and writes a
small diagnostics bundle for inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright

from config.settings import get_settings
from recording.ffmpeg_pipeline import FFmpegPipeline
from recording.runtime_checks import get_recording_runtime_status
from recording.virtual_env import VirtualEnvironment, VirtualEnvironmentConfig
from utils.timezone import utc_now

logger = logging.getLogger(__name__)


SMOKE_PAGE_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Recording Smoke</title>
    <style>
      html, body {
        margin: 0;
        width: 100%;
        height: 100%;
        overflow: hidden;
        background:
          radial-gradient(circle at 20% 20%, #ffd166 0%, transparent 30%),
          radial-gradient(circle at 80% 30%, #06d6a0 0%, transparent 25%),
          linear-gradient(135deg, #0b132b 0%, #1c2541 100%);
        color: #f8f9fa;
        font-family: "Trebuchet MS", sans-serif;
      }
      #clock {
        position: absolute;
        top: 24px;
        left: 24px;
        font-size: 36px;
        letter-spacing: 0.08em;
      }
      #status {
        position: absolute;
        top: 76px;
        left: 24px;
        font-size: 18px;
        opacity: 0.85;
      }
      canvas {
        width: 100%;
        height: 100%;
      }
    </style>
  </head>
  <body>
    <div id="clock">booting…</div>
    <div id="status">Animating deterministic smoke scene</div>
    <canvas id="scene" width="1920" height="1080"></canvas>
    <script>
      const canvas = document.getElementById("scene");
      const ctx = canvas.getContext("2d");
      let frame = 0;

      function draw() {
        frame += 1;
        const w = canvas.width;
        const h = canvas.height;
        const hue = frame % 360;
        ctx.fillStyle = `hsl(${hue} 55% 20%)`;
        ctx.fillRect(0, 0, w, h);

        for (let i = 0; i < 7; i += 1) {
          const size = 90 + i * 35;
          const x = ((frame * (i + 2) * 5) % (w + size * 2)) - size;
          const y = h * (0.15 + i * 0.1);
          ctx.fillStyle = `hsla(${(hue + i * 40) % 360} 85% 62% / 0.75)`;
          ctx.fillRect(x, y, size, size);
        }

        ctx.strokeStyle = "rgba(255,255,255,0.22)";
        ctx.lineWidth = 3;
        ctx.beginPath();
        for (let x = 0; x <= w; x += 24) {
          const y = h * 0.55 + Math.sin((x + frame * 8) / 65) * 95;
          if (x === 0) {
            ctx.moveTo(x, y);
          } else {
            ctx.lineTo(x, y);
          }
        }
        ctx.stroke();

        requestAnimationFrame(draw);
      }

      async function startAudio() {
        const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextCtor) {
          return false;
        }
        window.__smokeAudio = new AudioContextCtor();
        await window.__smokeAudio.resume();
        const oscillator = window.__smokeAudio.createOscillator();
        oscillator.type = "triangle";
        oscillator.frequency.value = 440;
        const gain = window.__smokeAudio.createGain();
        gain.gain.value = 0.03;
        const lfo = window.__smokeAudio.createOscillator();
        const lfoGain = window.__smokeAudio.createGain();
        lfo.frequency.value = 0.5;
        lfoGain.gain.value = 110;
        lfo.connect(lfoGain);
        lfoGain.connect(oscillator.frequency);
        oscillator.connect(gain);
        gain.connect(window.__smokeAudio.destination);
        oscillator.start();
        lfo.start();
        return true;
      }

      function tickClock() {
        document.getElementById("clock").textContent = new Date().toISOString();
      }

      tickClock();
      setInterval(tickClock, 250);
      draw();
      window.addEventListener("load", () => {
        startAudio().catch(() => {});
      });
    </script>
  </body>
</html>
"""


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run a deterministic recording smoke test")
    parser.add_argument("--duration-sec", type=int, default=8, help="Recording duration in seconds")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.recordings_dir / "smoke",
        help="Directory for the output recording",
    )
    parser.add_argument(
        "--diagnostics-dir",
        type=Path,
        default=settings.diagnostics_dir / "smoke",
        help="Directory for smoke diagnostics",
    )
    return parser.parse_args()


def write_json(path: Path, payload: dict) -> None:
    """Write a UTF-8 JSON file with indentation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


async def main() -> int:
    """Run the smoke test and return a process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    settings = get_settings()

    output_dir = args.output_dir
    diagnostics_dir = args.diagnostics_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    diagnostics_dir.mkdir(parents=True, exist_ok=True)

    runtime_path = diagnostics_dir / "runtime.json"
    metadata_path = diagnostics_dir / "metadata.json"
    screenshot_path = diagnostics_dir / "smoke.png"
    ffmpeg_log_path = diagnostics_dir / "ffmpeg.log"
    output_path = output_dir / "recording_smoke.mkv"

    runtime_status = get_recording_runtime_status()
    summary: dict = {
        "started_at": utc_now().isoformat(),
        "duration_sec": args.duration_sec,
        "output_path": str(output_path),
        "ffmpeg_log_path": str(ffmpeg_log_path),
        "runtime_status": runtime_status,
        "success": False,
    }

    if not runtime_status["ready"]:
        summary["error"] = "Recording runtime is not ready"
        write_json(runtime_path, summary)
        write_json(
            metadata_path,
            {
                "collected_at": utc_now().isoformat(),
                "stage": "readiness",
                "error": summary["error"],
                "runtime_status": runtime_status,
            },
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 2

    virtual_env: VirtualEnvironment | None = None
    playwright = None
    browser = None
    context = None
    page = None
    ffmpeg: FFmpegPipeline | None = None

    try:
        virtual_env = VirtualEnvironment(
            config=VirtualEnvironmentConfig(
                width=settings.resolution_w,
                height=settings.resolution_h,
            )
        )
        env_vars = await virtual_env.start()

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--autoplay-policy=no-user-gesture-required",
                "--hide-scrollbars",
                "--disable-infobars",
                f"--window-size={settings.resolution_w},{settings.resolution_h}",
                "--window-position=0,0",
                "--app=about:blank",
            ],
            env=env_vars,
        )
        context = await browser.new_context(
            viewport={"width": settings.resolution_w, "height": settings.resolution_h},
            permissions=["microphone"],
        )
        page = await context.new_page()
        await page.set_content(SMOKE_PAGE_HTML, wait_until="load")
        await page.evaluate("window.startAudio && window.startAudio()")
        await page.screenshot(path=str(screenshot_path), full_page=True)

        write_json(
            metadata_path,
            {
                "collected_at": utc_now().isoformat(),
                "stage": "start_capture",
                "title": await page.title(),
                "url": page.url,
                "viewport": page.viewport_size,
            },
        )

        ffmpeg = FFmpegPipeline(
            output_path=output_path,
            display=virtual_env.display,
            audio_source=virtual_env.pulse_monitor,
            width=settings.resolution_w,
            height=settings.resolution_h,
            log_path=ffmpeg_log_path,
        )
        await ffmpeg.start()
        await asyncio.sleep(args.duration_sec)
        recording_info = await ffmpeg.stop()
        ffmpeg = None

        summary.update(
            {
                "success": recording_info.file_size > 0 and ffmpeg_log_path.exists(),
                "recording_info": {
                    "output_path": str(recording_info.output_path),
                    "file_size": recording_info.file_size,
                    "duration_sec": recording_info.duration_sec,
                    "start_time": recording_info.start_time.isoformat(),
                    "end_time": recording_info.end_time.isoformat(),
                },
                "completed_at": utc_now().isoformat(),
            }
        )
        if not summary["success"]:
            summary["error"] = "Smoke recording did not produce a valid output/log pair"

        write_json(runtime_path, summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if summary["success"] else 1

    except Exception as exc:
        summary.update(
            {
                "completed_at": utc_now().isoformat(),
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        )
        write_json(runtime_path, summary)
        if not metadata_path.exists():
            write_json(
                metadata_path,
                {
                    "collected_at": utc_now().isoformat(),
                    "stage": "exception",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 1

    finally:
        if ffmpeg and ffmpeg.is_recording:
            try:
                await ffmpeg.stop()
            except Exception:
                logger.exception("Failed to stop FFmpeg during smoke cleanup")

        if context:
            try:
                await context.close()
            except Exception:
                logger.exception("Failed to close browser context during smoke cleanup")

        if browser:
            try:
                await browser.close()
            except Exception:
                logger.exception("Failed to close browser during smoke cleanup")

        if playwright:
            try:
                await playwright.stop()
            except Exception:
                logger.exception("Failed to stop Playwright during smoke cleanup")

        if virtual_env:
            try:
                await virtual_env.stop()
            except Exception:
                logger.exception("Failed to stop virtual environment during smoke cleanup")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
