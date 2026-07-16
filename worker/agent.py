"""Farmhand's single-process Blender worker."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

FIVE_GIB = 5 * 1024**3
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RESULT_RETRY_DELAYS = (2, 8, 30)
GPU_TYPES = {"OPTIX", "CUDA", "HIP", "METAL", "NONE"}


@dataclass(frozen=True)
class Config:
    coordinator_url: str
    token: str
    worker_id: str
    blender_path: str
    work_dir: Path
    poll_interval: float = 10
    gpu: str = "NONE"


def load_config(path: Path) -> Config:
    with path.open("rb") as handle:
        values = tomllib.load(handle)
    values["work_dir"] = Path(values["work_dir"]).expanduser()
    values["gpu"] = values.get("gpu", "NONE").upper()
    if values["gpu"] not in GPU_TYPES:
        raise ValueError(f"gpu must be one of: {', '.join(sorted(GPU_TYPES))}")
    return Config(**values)


def detect_hardware(runner: Callable[..., Any] = subprocess.run) -> dict[str, str]:
    """Best-effort OS/CPU/GPU names for the dashboard. Empty strings on failure."""

    def run(command: list[str]) -> str:
        result = runner(command, capture_output=True, text=True, timeout=15)
        return result.stdout if isinstance(result.stdout, str) else ""

    system = platform.system()
    info = {
        "os": f"{system} {platform.release()}".strip(),
        "cpu": platform.processor() or platform.machine(),
        "gpu": "",
    }
    try:
        if system == "Darwin":
            info["cpu"] = run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or info["cpu"]
            lines = run(["system_profiler", "SPDisplaysDataType"]).splitlines()
            info["gpu"] = next(
                (line.split(":", 1)[1].strip() for line in lines if "Chipset Model" in line), ""
            )
        elif system == "Windows":
            lines = [
                line.strip()
                for line in run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "(Get-CimInstance Win32_Processor).Name; "
                        "(Get-CimInstance Win32_VideoController).Name",
                    ]
                ).splitlines()
                if line.strip()
            ]
            if lines:
                info["cpu"] = lines[0]
                info["gpu"] = ", ".join(lines[1:])
        elif system == "Linux":
            cpuinfo = Path("/proc/cpuinfo").read_text().splitlines()
            info["cpu"] = next(
                (line.split(":", 1)[1].strip() for line in cpuinfo if "model name" in line),
                info["cpu"],
            )
            lines = run(["lspci"]).splitlines()
            info["gpu"] = next(
                (
                    line.split(":", 2)[-1].strip()
                    for line in lines
                    if "VGA" in line or "3D controller" in line
                ),
                "",
            )
    except Exception:  # ponytail: dashboard cosmetics; never block rendering on detection
        pass
    return info


def get_blender_version(path: str, runner: Callable[..., Any] = subprocess.run) -> str:
    result = runner([path, "--version"], capture_output=True, text=True, check=True)
    output = (
        result.stdout.decode(errors="replace")
        if isinstance(result.stdout, bytes)
        else result.stdout
    )
    match = re.search(r"Blender\s+(\d+\.\d+)", output or "")
    if not match:
        raise RuntimeError("could not parse Blender version")
    return match.group(1)


def build_blender_command(config: Config, blend: Path, output: Path, frame: int) -> list[str]:
    command = [config.blender_path, "-b", str(blend)]
    if config.gpu != "NONE":
        command += [
            "--python-expr",
            "\n".join(
                (
                    "import bpy",
                    "prefs = bpy.context.preferences.addons['cycles'].preferences",
                    f"prefs.compute_device_type = '{config.gpu}'",
                    "prefs.get_devices()",
                    "for d in prefs.devices: d.use = True",
                    "bpy.context.scene.cycles.device = 'GPU'",
                )
            ),
        ]
    return command + ["-o", str(output), "-F", "PNG", "-noaudio", "-f", str(frame)]


def _bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode(errors="replace")


class Worker:
    def __init__(
        self,
        config: Config,
        *,
        client: Any = None,
        runner: Callable[..., Any] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        log: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.runner, self.sleep, self.disk_usage, self.log = runner, sleep, disk_usage, log
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(
            base_url=config.coordinator_url.rstrip("/"),
            headers={"X-Farm-Token": config.token},
            timeout=60,
        )
        self.blender_version = get_blender_version(config.blender_path, runner)
        self.hardware = detect_hardware(runner)

    def ensure_blend(self, work: dict[str, Any]) -> Path:
        if work.get("blend_path"):
            shared = Path(work["blend_path"])
            if not shared.is_file():
                raise RuntimeError(f"shared blend not found on this worker: {shared}")
            return shared
        cache = self.config.work_dir / "cache"
        cache.mkdir(exist_ok=True)
        expected = work["blend_sha256"].lower()
        target = cache / f"{expected}.blend"
        if target.exists() and self._digest(target) == expected:
            target.touch()
            self._evict(cache)
            return target
        target.unlink(missing_ok=True)
        if self.disk_usage(self.config.work_dir).free < FIVE_GIB:
            raise RuntimeError("less than 5 GiB free; refusing blend download")
        part = target.with_suffix(".blend.part")
        for attempt in range(2):
            response = self.client.get(work["blend_url"])
            response.raise_for_status()
            part.write_bytes(response.content)
            if self._digest(part) == expected:
                os.replace(part, target)
                self._evict(cache)
                return target
            part.unlink(missing_ok=True)
            if attempt == 1:
                raise RuntimeError("blend SHA-256 mismatch after retry")
        raise AssertionError("unreachable")

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _evict(cache: Path) -> None:
        entries = sorted(
            cache.glob("*.blend"), key=lambda path: path.stat().st_mtime_ns, reverse=True
        )
        for path in entries[3:]:
            path.unlink(missing_ok=True)

    def _post_with_retry(self, url: str, **kwargs: Any) -> None:
        for attempt in range(len(RESULT_RETRY_DELAYS) + 1):
            try:
                response = self.client.post(url, **kwargs)
                response.raise_for_status()
                return
            except httpx.HTTPError:
                if attempt == len(RESULT_RETRY_DELAYS):
                    raise
                self.sleep(RESULT_RETRY_DELAYS[attempt])

    def _report_failure(self, work: dict[str, Any], exit_code: int, tail: str) -> None:
        self._post_with_retry(
            f"/jobs/{work['job_id']}/frames/{work['frame']}/result",
            json={
                "status": "failed",
                "worker_id": self.config.worker_id,
                "exit_code": exit_code,
                "stderr_tail": tail,
            },
        )

    def process(self, work: dict[str, Any]) -> None:
        frame = int(work["frame"])
        output_dir = self.config.work_dir / "out"
        output_dir.mkdir(exist_ok=True)
        pattern = output_dir / "frame_####"
        output = output_dir / f"frame_{frame:04d}.png"
        started = time.monotonic()
        try:
            try:
                blend = self.ensure_blend(work)
            except httpx.HTTPError:
                raise
            except (OSError, RuntimeError) as error:
                self._report_failure(work, -1, str(error)[-4096:])
                return
            try:
                result = self.runner(
                    build_blender_command(self.config, blend, pattern, frame),
                    capture_output=True,
                    timeout=max(1, int(work["lease_seconds"]) - 60),
                )
                tail = (_bytes(result.stdout) + b"\n" + _bytes(result.stderr))[-4096:].decode(
                    errors="replace"
                )
                valid = result.returncode == 0 and self._valid_png(output)
                if not valid:
                    self._report_failure(
                        work, result.returncode, tail or "invalid or missing PNG output"
                    )
                    return
            except subprocess.TimeoutExpired as error:
                tail = (_bytes(error.output) + b"\n" + _bytes(error.stderr))[-4096:].decode(
                    errors="replace"
                )
                self._report_failure(work, -1, tail or "Blender render timed out")
                return
            except OSError as error:
                self._report_failure(work, -1, str(error)[-4096:])
                return
            self._post_with_retry(
                f"/jobs/{work['job_id']}/frames/{frame}/result",
                data={
                    "worker_id": self.config.worker_id,
                    "render_seconds": f"{time.monotonic() - started:.3f}",
                },
                files={"frame_file": (output.name, output.read_bytes(), "image/png")},
            )
        finally:
            output.unlink(missing_ok=True)

    @staticmethod
    def _valid_png(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= len(PNG_SIGNATURE):
            return False
        with path.open("rb") as handle:
            return handle.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE

    def run_forever(self, *, max_polls: int | None = None) -> None:
        polls = 0
        while max_polls is None or polls < max_polls:
            polls += 1
            try:
                response = self.client.get(
                    "/work",
                    params={
                        "worker_id": self.config.worker_id,
                        "blender_version": self.blender_version,
                        **self.hardware,
                    },
                )
                if response.status_code == 204:
                    self.sleep(self.config.poll_interval)
                    continue
                response.raise_for_status()
                self.process(response.json())
            except httpx.HTTPError as error:
                self.log(f"coordinator error: {error}")
                self.sleep(self.config.poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("worker/config.toml"))
    args = parser.parse_args()
    worker = Worker(load_config(args.config))
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        close = getattr(worker.client, "close", None)
        if close:
            close()


if __name__ == "__main__":
    main()
