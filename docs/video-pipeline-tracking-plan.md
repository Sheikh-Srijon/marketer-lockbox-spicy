# Video Pipeline Tracking Plan

## Goal

Track each source video through the marketing funnel:

```text
source_acquired -> ai_video_started -> ai_video_finished -> captioned -> posted
```

The TikTok/Apify `source_video_id` remains the source of truth for lineage. Each AI-generated base video and each final captioned output video gets its own deterministic ID so the pipeline can resume, debug failures, and avoid duplicate work.

## Storage Layout

Organize files by pipeline stage under `videos/`.

```text
videos/
  input/
    source/
      {source_video_id}.mp4

  ai/
    base/
      {ai_video_id}.mp4
      {ai_video_id}.json

  drafted/
    captioned/
      {output_video_id}.mp4
      {output_video_id}.json

  posted/
    {output_video_id}.mp4

  metadata/
    video_pipeline.csv
    source_videos.jsonl
```

### Stage Storage

`source_acquired`

Store the source TikTok or pre-seed MP4:

```text
videos/input/source/{source_video_id}.mp4
```

`ai_video_started`

No persistent video file exists yet. If the source is longer than the Kling motion-control target length, create a temporary trimmed motion reference for the API call and delete it after the call completes. Track the stage in:

```text
videos/metadata/video_pipeline.csv
```

Set `ai_stage_status=started` and store `ai_task_id` as soon as Kling returns the task id.

`ai_video_finished`

Store the Kling-generated base video and sidecar:

```text
videos/ai/base/{ai_video_id}.mp4
videos/ai/base/{ai_video_id}.json
```

`captioned`

Store final postable caption variants:

```text
videos/drafted/captioned/{output_video_id}.mp4
videos/drafted/captioned/{output_video_id}.json
```

`posted`

After posting, copy the exact posted asset:

```text
videos/posted/{output_video_id}.mp4
```

Prefer copy over move so `videos/drafted/captioned/` remains the stable final-output archive and `videos/posted/` is just the published subset.

## Pipeline Ledger

Add a lightweight ledger:

```text
videos/metadata/video_pipeline.csv
```

Track one row per final captioned output variant.

```csv
source_video_id,
ai_video_id,
ai_stage_status,
ai_task_id,
ai_output_path,
output_video_id,
variant_number,
caption_hash,
caption_stage_status,
captioned_output_path,
post_stage_status,
last_error,
updated_at_utc
```

## ID Strategy

Use deterministic IDs so reruns can find the same assets.

```text
ai_video_id = ai_{source_video_id}_{character_handle}_{run_date}
```

Example:

```text
ai_7625969053261352212_stella_20260614
```

```text
output_video_id = out_{source_video_id}_{character_handle}_v{variant_number}_{caption_hash}
```

Example:

```text
out_7625969053261352212_stella_v01_a13f92c8
```

Use a short stable hash of the full caption, not only `caption_slug`, because different captions can collapse into the same slug.

## Ledger Update Points

After source acquisition:

- Create or prepare expected rows for all variants.
- Set `ai_stage_status=not_started`.
- Set `caption_stage_status=not_started`.
- Set `post_stage_status=not_started`.

Right after Kling returns `task_id`:

- Set `ai_stage_status=started`.
- Store `ai_task_id`.
- Update `updated_at_utc`.

After Kling output MP4 downloads:

- Set `ai_stage_status=finished`.
- Store `ai_output_path`.
- Update `updated_at_utc`.

After each captioned variant is written:

- Set `caption_stage_status=finished`.
- Store `captioned_output_path`.
- Update `updated_at_utc`.

Later, when posting is wired in:

- Set `post_stage_status=posted`.
- Copy the posted MP4 into `videos/posted/`.
- Update `updated_at_utc`.

## Failure Handling

Catch failures at the stage boundary in the orchestrator.

On failure:

- Set the relevant stage status to `failed`.
- Store a short `last_error`.
- Update `updated_at_utc`.
- Re-raise the exception so the command still fails visibly.

This gives debug visibility without hiding broken runs.

## Helper Functions

Keep CSV and ID logic centralized with small helpers:

```text
make_ai_video_id(...)
make_output_video_id(...)
caption_hash(...)
upsert_pipeline_row(...)
mark_pipeline_stage(...)
```

Avoid spreading raw CSV writes across the pipeline.

## Scope Boundary

Keep this scoped to orchestration.

- `videos/metadata.csv` remains the raw Apify/source metadata file.
- `videos/metadata/video_pipeline.csv` becomes the marketer pipeline tracker.
- The raw TikTok downloader should not become responsible for the full funnel.

## V1 Acceptance Criteria

- Every source video selected by the agent can be traced by `source_video_id`.
- Every Kling task that starts is recorded with `ai_task_id`.
- Every successful Kling base output has a deterministic `ai_video_id` and path.
- Every captioned output has a deterministic `output_video_id` and path.
- Failed stages leave a row with `failed` and a useful `last_error`.
- Rerunning the agent can inspect the ledger and avoid duplicating finished work.
