# Farmhand

Farmhand is a small, pull-based Blender render farm for a trusted local network.
One coordinator stores jobs and frames, workers ask for one frame at a time, and
the optional Blender add-on packs and submits the current scene. The status
dashboard is served by the coordinator at `http://<coordinator-ip>:8420/`.

Farmhand v1 renders PNG frame sequences. Read [the API contract](shared/API.md)
for the authoritative HTTP behavior.

## Requirements and installation

- Python 3.11 or newer on the coordinator and every worker.
- Blender installed on every worker.
- The same Blender **major.minor** version on the submitting workstation and
  compatible workers. For example, a 4.5 job is not assigned to a 4.2 worker.
- LAN reachability from workers to TCP port 8420 on the coordinator.

Create a separate virtual environment on each coordinator or worker machine:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On Windows PowerShell, use:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

The `dev` extra installs the test tools. A runtime-only machine may instead use
`python -m pip install -e .`.

## Start the coordinator

Choose one long random token, store it securely, and use the exact same value
in every worker config and Blender add-on. Set a persistent data directory for
the SQLite database, uploaded blends, and completed frames:

```bash
export FARMHAND_TOKEN='EDIT_ME_use_a_long_random_token'
export FARMHAND_DATA_DIR="$PWD/.farmhand-data"
mkdir -p "$FARMHAND_DATA_DIR"
chmod 700 "$FARMHAND_DATA_DIR"

uvicorn coordinator.main:app --host 0.0.0.0 --port 8420
```

Generate a token once with
`python -c 'import secrets; print(secrets.token_urlsafe(32))'` if needed. Do not
generate a new token at every launch: existing worker and add-on settings would
stop authenticating.

Open `http://127.0.0.1:8420/` locally or
`http://<coordinator-lan-ip>:8420/` from another LAN machine. The dashboard asks
for the shared token and keeps it in that browser's local storage. A quick
health check is:

```bash
export TOKEN="$FARMHAND_TOKEN"
curl -fsS http://127.0.0.1:8420/jobs -H "X-Farm-Token: $TOKEN"
```

### LAN-only security boundary

The coordinator deliberately listens on all interfaces and uses plain HTTP.
The shared token prevents accidental access; it is not a defense against a
hostile network peer. Restrict port 8420 to your private LAN in the host
firewall. Never port-forward it, expose it to the public internet, or run it on
an untrusted Wi-Fi/VPN. Anyone who learns the token and can reach the port can
submit jobs, download blends and frames, or cancel work.

## Configure and run a worker

Copy the worker template on each worker, then replace every example value with
values for that machine:

```bash
cp worker/config.example.toml worker/config.toml
```

```toml
coordinator_url = "http://192.168.1.50:8420"
token = "EDIT_ME_same_token_as_the_coordinator"
worker_id = "shop-pc"
blender_path = "/absolute/path/to/blender"
work_dir = "/absolute/path/to/farmhand-worker-data"
poll_interval = 10
gpu = "OPTIX"
```

Use an absolute `blender_path` (including `blender.exe` on Windows), a unique,
human-readable `worker_id`, and a writable `work_dir`. Start the worker from the
repository root:

```bash
.venv/bin/python -m worker.agent --config worker/config.toml
```

On Windows the equivalent is:

```powershell
.\.venv\Scripts\python.exe -m worker.agent --config worker\config.toml
```

The worker tolerates coordinator/network outages, sleeps for `poll_interval`
when no compatible work exists, and caches the three most recent packed blends.

### GPU and headless behavior

`gpu` accepts `OPTIX`, `CUDA`, `HIP`, `METAL`, or `NONE` (case-insensitive).
Choose the backend supported by that worker's Blender build and hardware:

- `OPTIX` or `CUDA`: NVIDIA; OPTIX also requires a compatible driver.
- `HIP`: supported AMD configurations.
- `METAL`: Apple silicon and supported macOS GPUs.
- `NONE`: CPU rendering, useful for CPU-only machines or scenes that exceed GPU
  VRAM.

Blender's `-b` headless mode does not inherit the GPU choice made in the desktop
preferences. Farmhand configures the selected Cycles device inside every
headless invocation. If the backend or driver is unavailable, or the scene runs
out of VRAM, the frame failure and Blender's stderr tail appear in job status.
Confirm GPU utilization and compare a test frame's render time before trusting
a new worker. CPU fallback can be slower and may produce visibly different
noise, so inspect mixed-device sequences.

## Install the Blender add-on

Build a zip whose root contains the `farmhand_submit` package:

```bash
cd addon
python -m zipfile -c ../farmhand_submit.zip farmhand_submit
cd ..
```

In Blender, open **Edit > Preferences > Add-ons**, choose **Install from
Disk...**, select `farmhand_submit.zip`, and enable Farmhand. Set **Coordinator
URL** and **Farm Token** in the add-on preferences. The submission UI is in
**3D Viewport > Sidebar (N) > Farmhand**.

Save the scene before submitting it. The add-on packs supported resources into
a temporary copy, submits that copy, and leaves the working file in place. Its
panel supports the scene frame range or a custom range, **Render on Farm**,
refresh/cancel controls, and progress/state counts.

### Packed-asset limitations

Blender's Pack Resources embeds images and some datablocks, but it does not make
every scene self-contained. In particular, these do not reliably travel in a
v1 job:

- linked `.blend` libraries;
- fluid, cloth, and other simulation caches stored in external directories;
- fonts installed only on the submitting operating system; and
- absolute-path video textures or other external files.

The add-on blocks known unsupported external references, but that check cannot
prove every scene is portable. Make linked data local, bake caches into portable
data where Blender supports it, and test one representative frame on a worker
before submitting a long job.

## Curl smoke playbook

This exercises the state machine without the add-on or a real worker. First use
Blender's **File > External Data > Pack Resources**, save a copy as
`test_scene.blend`, and have any valid PNG available as `any.png`. Use the
Blender major.minor installed on the machine, such as `4.5`:

```bash
export BASE_URL=http://127.0.0.1:8420
export TOKEN="$FARMHAND_TOKEN"

RESPONSE=$(curl -fsS -X POST "$BASE_URL/jobs" \
  -H "X-Farm-Token: $TOKEN" \
  -F "blend_file=@test_scene.blend" \
  -F 'params={"name":"smoke test","frame_start":1,"frame_end":10,"frame_step":1,"engine":"CYCLES","output_format":"PNG","blender_version":"4.5"}')
printf '%s\n' "$RESPONSE"
JOB_ID=$(printf '%s' "$RESPONSE" | python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

# Claim frame 1 as a fake worker.
curl -fsS "$BASE_URL/work?worker_id=fake&blender_version=4.5" \
  -H "X-Farm-Token: $TOKEN"

# Complete the claimed frame with a valid PNG.
curl -fsS -X POST "$BASE_URL/jobs/$JOB_ID/frames/1/result" \
  -H "X-Farm-Token: $TOKEN" \
  -F "worker_id=fake" \
  -F "render_seconds=0.1" \
  -F "frame_file=@any.png"

# Inspect the job and retrieve all completed frames currently available.
curl -fsS "$BASE_URL/jobs/$JOB_ID" -H "X-Farm-Token: $TOKEN"
curl -fsS "$BASE_URL/jobs/$JOB_ID/frames.zip" \
  -H "X-Farm-Token: $TOKEN" -o frames.zip
```

The worker claim and submitted job must use the same Blender major.minor. A
mismatch intentionally returns no work. For an actual end-to-end render, omit
the fake claim/result steps and start one or more configured workers instead.

## Retrieve finished frames

The dashboard offers the completed-frame archive. The equivalent API call is:

```bash
curl -fsS "http://127.0.0.1:8420/jobs/$JOB_ID/frames.zip" \
  -H "X-Farm-Token: $TOKEN" -o "$JOB_ID-frames.zip"
```

`frames.zip` contains the completed PNG files. It can also be downloaded while
a job is still active, in which case only frames already completed are present.

## Run a worker at boot

The templates intentionally contain `EDIT_ME` identities and paths and cannot
be installed unchanged. Put the repository, virtual environment, Blender, and
worker data somewhere the selected service account can read or write as
appropriate. Test the foreground worker command successfully before enabling a
boot service.

### Linux systemd

1. Copy `packaging/farmhand-worker.service` to
   `/etc/systemd/system/farmhand-worker.service`.
2. Edit every `EDIT_ME` value. `User` must be an existing, unprivileged account;
   `WorkingDirectory`, the Python path, and config path must be absolute.
3. Ensure that account can execute Blender and write the configured `work_dir`.
4. Load, enable, and inspect the worker:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now farmhand-worker.service
sudo systemctl status farmhand-worker.service
sudo journalctl -u farmhand-worker.service -f
```

After later edits, run `sudo systemctl daemon-reload` and
`sudo systemctl restart farmhand-worker.service`.

### Windows Task Scheduler

1. Copy `packaging/FarmhandWorkerTask.xml` and replace every `EDIT_ME` identity
   and path. Keep the Python executable, config, and working directory absolute.
2. In Task Scheduler choose **Import Task...**, select the edited XML, then use
   **Change User or Group...** to select a dedicated, non-administrator account.
3. Select **Run whether user is logged on or not** and provide that account's
   password when saving; credentials are deliberately not stored in the XML.
4. Confirm the account can run Blender and write the configured `work_dir`, then
   right-click **Farmhand Worker**, choose **Run**, and inspect **History**.

The task has a startup trigger, waits 30 seconds for networking, and restarts
after failures. Do not disable password storage or substitute an interactive
logon trigger if the worker must start before anyone signs in.

## Failure drills

Run these before trusting the farm with production work:

1. **Worker power loss:** submit a multi-frame job and terminate a worker
   mid-render (power off or `kill -9`). After the lease expires and the next
   30-second sweep runs, confirm the frame returns to pending, another worker
   claims it, and the job completes.
2. **Coordinator restart:** restart the coordinator mid-job with the same
   `FARMHAND_DATA_DIR` and token. Confirm job/frame state survives in SQLite and
   workers resume polling without being restarted.
3. **Bad scene:** submit a scene with a missing linked library or external
   cache. Confirm the add-on blocks submission with a useful reference list;
   make the data local or bake it before retrying.
4. **Slow or interrupted upload:** throttle or briefly disconnect a worker while
   it uploads a rendered frame. Confirm the worker's result-upload retries
   succeed and the coordinator records one completed frame, not duplicates.
5. **Overnight render:** render a genuine 200+ frame animation on every worker.
   Download `frames.zip`, scrub the entire sequence, and look for missing frames,
   wrong settings, device-specific noise, suspiciously slow CPU frames, and
   failed-frame stderr before considering the setup production-ready.

Also test a deliberate GPU out-of-memory failure on any marginal worker. It
should produce a readable stderr tail; switch that worker to `gpu = "NONE"` or
remove it from scenes that exceed its VRAM.

## Explicitly out of scope for v1

- Automatic worker discovery (mDNS or UDP broadcast); worker configs use the
  coordinator's LAN IP.
- Frame chunking or tile splitting; one claim is one frame.
- EXR, multilayer, or video output; v1 output is PNG only.
- A worker desktop/Tauri application; the worker is a daemon and observability
  lives in the web dashboard.
- Multi-user authentication, HTTPS, or internet exposure.
- Bundling and path rewriting for linked libraries or external directories.
- Priority queues or concurrent-job racing; jobs are claimed FIFO by creation
  time.

## Tests

```bash
python -m pytest
```
