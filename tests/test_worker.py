from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from stub_server import create_app
from worker.agent import (
    Config,
    Worker,
    build_blender_command,
    get_blender_version,
    load_config,
)

PNG = b"\x89PNG\r\n\x1a\nrendered"


class Response:
    def __init__(self, status_code=200, *, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://farm.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("bad response", request=request, response=response)


class Client:
    def __init__(self, gets=(), posts=()):
        self.gets = list(gets)
        self.posts = list(posts)
        self.calls = []
        self.uploads = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        response = self.gets.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if "files" in kwargs:
            body = kwargs["files"]["frame_file"][1]
            self.uploads.append(body if isinstance(body, bytes) else body.read())
        response = self.posts.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def config(tmp_path: Path, **overrides) -> Config:
    values = {
        "coordinator_url": "http://farm.test",
        "token": "secret",
        "worker_id": "worker-one",
        "blender_path": "/opt/blender",
        "work_dir": tmp_path,
        "poll_interval": 10.0,
        "gpu": "OPTIX",
    }
    values.update(overrides)
    return Config(**values)


def assignment(data: bytes, frame: int = 147):
    return {
        "job_id": "abc123",
        "frame": frame,
        "blend_sha256": hashlib.sha256(data).hexdigest(),
        "blend_url": "/jobs/abc123/blend",
        "output_format": "PNG",
        "engine": "CYCLES",
        "lease_seconds": 1800,
    }


def version_runner(args, **kwargs):
    assert args == ["/opt/blender", "--version"]
    return SimpleNamespace(stdout="Blender 4.5.3\n", stderr="", returncode=0)


def test_loads_toml_and_parses_blender_version(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'coordinator_url="http://farm.test"\ntoken="secret"\nworker_id="w"\n'
        f'blender_path="/opt/blender"\nwork_dir="{tmp_path}"\npoll_interval=7\ngpu="NONE"\n'
    )

    loaded = load_config(path)

    assert loaded.poll_interval == 7
    assert loaded.gpu == "NONE"
    assert loaded.work_dir == tmp_path
    assert get_blender_version(loaded.blender_path, version_runner) == "4.5"


def test_loads_and_applies_shared_path_mapping(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'coordinator_url="http://farm.test"\ntoken="secret"\nworker_id="w"\n'
        f'blender_path="/opt/blender"\nwork_dir="{tmp_path}"\ngpu="NONE"\n'
        'shared_path_from="/Volumes/AviatorPro"\n'
        'shared_path_to="//Aviator-Pro-NAS/AviatorPro"\n'
    )

    loaded = load_config(path)

    assert loaded.map_shared_path(
        "/Volumes/AviatorPro/_2 Aviator Pro/scene.blend"
    ) == "//Aviator-Pro-NAS/AviatorPro/_2 Aviator Pro/scene.blend"


def test_blender_command_keeps_render_flags_before_frame(tmp_path):
    cfg = config(tmp_path)

    command = build_blender_command(
        cfg, tmp_path / "cache" / "abc.blend", tmp_path / "out" / "frame_####", 147
    )

    assert command[:2] == ["/opt/blender", "-b"]
    assert command.index("--python-expr") < command.index("-o")
    assert command[command.index("-o") + 1].endswith("frame_####")
    assert command[-5:] == ["-F", "PNG", "-noaudio", "-f", "147"]
    expression = command[command.index("--python-expr") + 1]
    assert "compute_device_type = 'OPTIX'" in expression
    assert "prefs.get_devices()" in expression
    assert "d.use = True" in expression
    assert "scene.cycles.device = 'GPU'" in expression

    cpu_command = build_blender_command(
        config(tmp_path, gpu="NONE"), tmp_path / "a.blend", tmp_path / "frame_####", 1
    )
    assert "--python-expr" not in cpu_command


def test_download_retries_bad_digest_and_evicts_to_three_entries(tmp_path):
    blend = b"good blend"
    work = assignment(blend)
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(3):
        old = cache / f"old-{index}.blend"
        old.write_bytes(str(index).encode())
        old.touch()
    client = Client(gets=[Response(content=b"corrupt"), Response(content=blend)])
    worker = Worker(config(tmp_path), client=client, runner=version_runner)

    path = worker.ensure_blend(work)

    assert path.read_bytes() == blend
    assert len(list(cache.glob("*.blend"))) == 3
    assert not list(cache.glob("*.part"))
    assert [call[1] for call in client.calls] == [work["blend_url"], work["blend_url"]]


def test_shared_path_blend_is_used_without_download(tmp_path):
    nas = tmp_path / "nas" / "shot.blend"
    nas.parent.mkdir()
    nas.write_bytes(b"nas blend")
    work = assignment(b"nas blend")
    work["blend_path"] = str(nas)

    def runner(args, **kwargs):
        if args[-1] == "--version":
            return version_runner(args, **kwargs)
        if args[0] != "/opt/blender":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert args[2] == str(nas)  # renders straight off shared storage
        pattern = Path(args[args.index("-o") + 1])
        Path(str(pattern).replace("####", "0147") + ".png").write_bytes(PNG)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    client = Client(posts=[Response(200)])
    Worker(config(tmp_path), client=client, runner=runner).process(work)

    assert all(call[0] != "GET" for call in client.calls)
    assert client.uploads == [PNG]

    missing = dict(work, blend_path=str(tmp_path / "gone.blend"))
    client = Client(posts=[Response(200)])
    Worker(config(tmp_path), client=client, runner=runner).process(missing)
    payload = client.calls[-1][2]["json"]
    assert payload["status"] == "failed"
    assert "not found" in payload["stderr_tail"]


def test_disk_guard_refuses_download_below_five_gib(tmp_path):
    blend = b"blend"
    worker = Worker(
        config(tmp_path),
        client=Client(),
        runner=version_runner,
        disk_usage=lambda _: SimpleNamespace(free=5 * 1024**3 - 1),
    )

    with pytest.raises(RuntimeError, match="5 GiB"):
        worker.ensure_blend(assignment(blend))


def test_process_renders_valid_frame_uploads_with_retries_and_cleans_output(tmp_path):
    blend = b"blend data"
    work = assignment(blend)
    sleeps = []
    render_calls = []

    def runner(args, **kwargs):
        if args[-1] == "--version":
            return version_runner(args, **kwargs)
        if args[0] != "/opt/blender":  # hardware detection probes
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        render_calls.append((args, kwargs))
        pattern = Path(args[args.index("-o") + 1])
        Path(str(pattern).replace("####", "0147") + ".png").write_bytes(PNG)
        return SimpleNamespace(returncode=0, stdout=b"render log", stderr=b"")

    error = httpx.ConnectError("offline", request=httpx.Request("POST", "http://farm.test"))
    client = Client(
        gets=[Response(content=blend)],
        posts=[error, Response(503), Response(200)],
    )
    worker = Worker(config(tmp_path), client=client, runner=runner, sleep=sleeps.append)

    worker.process(work)

    assert sleeps == [2, 8]
    assert render_calls[0][1]["timeout"] == work["lease_seconds"] - 60
    post_calls = [call for call in client.calls if call[0] == "POST"]
    assert len(post_calls) == 3
    assert post_calls[-1][2]["data"]["worker_id"] == "worker-one"
    assert post_calls[-1][2]["files"]["frame_file"][0] == "frame_0147.png"
    assert client.uploads == [PNG, PNG, PNG]
    assert not (tmp_path / "out" / "frame_0147.png").exists()


def test_broken_render_reports_combined_last_4kb_and_cleans_output(tmp_path):
    blend = b"blend data"
    work = assignment(blend, frame=2)

    def runner(args, **kwargs):
        if args[-1] == "--version":
            return version_runner(args, **kwargs)
        output = Path(args[args.index("-o") + 1])
        Path(str(output).replace("####", "0002") + ".png").write_bytes(b"not a png")
        return SimpleNamespace(returncode=1, stdout=b"A" * 3000, stderr=b"B" * 3000)

    client = Client(gets=[Response(content=blend)], posts=[Response(200)])
    worker = Worker(config(tmp_path), client=client, runner=runner)

    worker.process(work)

    payload = client.calls[-1][2]["json"]
    assert payload["status"] == "failed"
    assert payload["exit_code"] == 1
    assert len(payload["stderr_tail"].encode()) <= 4096
    assert "B" * 100 in payload["stderr_tail"]
    assert not (tmp_path / "out" / "frame_0002.png").exists()


def test_poll_loop_parses_version_once_and_survives_network_error(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return version_runner(args, **kwargs)

    error = httpx.ConnectError("offline", request=httpx.Request("GET", "http://farm.test"))
    client = Client(gets=[error, Response(204)])
    sleeps = []
    worker = Worker(
        config(tmp_path, poll_interval=3), client=client, runner=runner, sleep=sleeps.append
    )

    worker.run_forever(max_polls=2)

    assert calls.count(["/opt/blender", "--version"]) == 1
    assert sleeps == [3, 3]
    work_calls = [call for call in client.calls if call[1] == "/work"]
    assert work_calls[-1][2]["params"]["blender_version"] == "4.5"


def test_timeout_is_reported_as_failure(tmp_path):
    blend = b"blend data"
    work = assignment(blend)

    def runner(args, **kwargs):
        if args[-1] == "--version":
            return version_runner(args, **kwargs)
        raise subprocess.TimeoutExpired(
            args, kwargs["timeout"], output=b"hung stdout", stderr=b"hung"
        )

    client = Client(gets=[Response(content=blend)], posts=[Response(200)])
    Worker(config(tmp_path), client=client, runner=runner).process(work)

    payload = client.calls[-1][2]["json"]
    assert payload["exit_code"] == -1
    assert "hung stdout" in payload["stderr_tail"]


def test_blender_launch_error_is_reported_instead_of_crashing_worker(tmp_path):
    blend = b"blend data"
    work = assignment(blend)

    def runner(args, **kwargs):
        if args[-1] == "--version":
            return version_runner(args, **kwargs)
        raise OSError("Blender executable disappeared")

    client = Client(gets=[Response(content=blend)], posts=[Response(200)])
    worker = Worker(config(tmp_path), client=client, runner=runner)

    worker.process(work)

    payload = client.calls[-1][2]["json"]
    assert payload["status"] == "failed"
    assert payload["exit_code"] == -1
    assert "executable disappeared" in payload["stderr_tail"]


def test_stub_claims_blend_and_accepts_png_before_next_frame(tmp_path):
    blend = tmp_path / "scene.blend"
    blend.write_bytes(b"BLENDER-v405 packed scene")
    app = create_app(blend, frames=[1, 2], token="secret", output_dir=tmp_path / "results")

    with TestClient(app) as client:
        assert client.get("/work?worker_id=tester&blender_version=4.5").status_code == 401
        headers = {"X-Farm-Token": "secret"}
        claim = client.get(
            "/work?worker_id=tester&blender_version=4.5", headers=headers
        )
        assert claim.status_code == 200
        assert claim.json()["frame"] == 1
        download = client.get(claim.json()["blend_url"], headers=headers)
        assert download.content == blend.read_bytes()
        assert download.headers["X-Blend-SHA256"] == hashlib.sha256(blend.read_bytes()).hexdigest()
        idle = client.get("/work?worker_id=tester&blender_version=4.5", headers=headers)
        assert idle.status_code == 204

        result = client.post(
            f"/jobs/{claim.json()['job_id']}/frames/1/result",
            headers=headers,
            data={"worker_id": "tester"},
            files={"frame_file": ("frame_0001.png", PNG, "image/png")},
        )

        assert result.status_code == 200
        assert (tmp_path / "results" / "frame_0001.png").read_bytes() == PNG
        next_claim = client.get(
            "/work?worker_id=tester&blender_version=4.5", headers=headers
        )
        assert next_claim.json()["frame"] == 2
