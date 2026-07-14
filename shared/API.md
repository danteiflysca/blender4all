# Farmhand API v1

Farmhand is a LAN-only Blender render farm. Every authenticated request carries
`X-Farm-Token`. JSON keys are `snake_case`, timestamps are UTC ISO-8601, job IDs
are UUID4 hex strings, and v1 output is PNG.

## Submit a job

`POST /jobs` accepts multipart fields:

- `blend_file`: a packed `.blend` file, streamed to disk.
- `params`: a JSON string containing `name`, `frame_start`, `frame_end`,
  `frame_step`, `output_format`, `engine`, and `blender_version`.

Only `PNG` is supported. Ranges must be ordered, have a positive step, and
contain at most 10,000 frames. Returns `201 {"job_id":"<uuid4 hex>"}`.

## Inspect and control jobs

- `GET /jobs` returns job summaries for the dashboard.
- `GET /jobs/{id}` returns job metadata, state counts, per-frame detail, and
  workers seen by this coordinator.
- `POST /jobs/{id}/cancel` marks the job cancelled. Pending frames will no
  longer be assigned.
- `POST /jobs/{id}/frames/{n}/requeue` resets a failed frame to pending and
  clears attempts and failure output.
- `GET /jobs/{id}/frames.zip` streams all completed PNG frames in a zip.

## Claim work

`GET /work?worker_id=X&blender_version=Y` atomically claims the oldest pending
frame from the oldest active, version-compatible job. It returns `204` when no
compatible work is available. A successful `200` response is:

```json
{
  "job_id": "a3f8c2...",
  "frame": 147,
  "blend_sha256": "9d41be...",
  "blend_url": "/jobs/a3f8c2.../blend",
  "output_format": "PNG",
  "engine": "CYCLES",
  "lease_seconds": 1800
}
```

The claim changes the frame from `pending` to `rendering`, records the worker,
and sets a lease. The coordinator matches Blender `major.minor` versions.

`GET /jobs/{id}/blend` streams the packed blend and includes its digest in the
`X-Blend-SHA256` response header.

## Submit a result

`POST /jobs/{id}/frames/{n}/result` requires `worker_id` and accepts either:

- multipart `frame_file` plus optional `render_seconds`; or
- JSON failure data:

```json
{
  "status": "failed",
  "worker_id": "shop-pc",
  "exit_code": 1,
  "stderr_tail": "Error: Out of GPU memory ..."
}
```

The worker must still hold the lease; stale or reassigned results return `409`.
A valid PNG is streamed to `storage/frames/{job_id}/frame_{n:04d}.png`, and the
frame becomes `done`. Failures increment attempts and requeue below three
attempts, otherwise they become `failed`. When every frame is done, the job
becomes `complete`.

## Frame states

- `pending -> rendering`: atomic worker claim.
- `rendering -> done`: valid result upload.
- `rendering -> pending`: expired lease or reported failure below three tries.
- `rendering -> failed`: third expired lease or third reported failure.

The coordinator sweeps expired leases every 30 seconds.

## Dashboard

`GET /` serves the static status dashboard. It polls job data every three
seconds and exposes frame state, workers, failures/requeue, ETA, cancellation,
and completed-frame download.
