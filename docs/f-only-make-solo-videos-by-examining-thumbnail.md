# Feature Idea: Only Make Solo Videos By Examining Thumbnail

## Goal

Only use TikTok source videos that appear to feature a single person. Before sending a downloaded TikTok video into Kling Motion Control, save and inspect its thumbnail so the pipeline can skip source videos with multiple people.

## Why

The marketer wants generated videos that map cleanly onto one character image. Multi-person source videos can produce confusing motion transfer, bad character identity, or unusable outputs. A thumbnail-based check is a lightweight first filter before doing more expensive video generation.

## Proposed Approach

When downloading TikTok videos through Apify:

- Extract the thumbnail URL from Apify metadata.
- Prefer `videoMeta.originalCoverUrl`.
- Fall back to `videoMeta.coverUrl`.
- Save the thumbnail image under `videos/thumbnails/`.
- Name it with the same timestamp/video ID convention as the MP4.

Example naming:

```text
videos/2026-04-07-105449_7625969053261352212.mp4
videos/thumbnails/2026-04-07-105449_7625969053261352212.jpg
```

Add metadata columns to `videos/metadata.csv`:

```text
thumbnail_path
thumbnail_url
thumbnail_original_url
solo_person_status
```

`solo_person_status` can start empty and later be filled with:

```text
solo
multiple
unknown
error
```

## Future Detection Step

Add a separate detector that reads each saved thumbnail and decides whether it contains one person or multiple people. The first version can run as a review/filter step after ingest. Later, it can run automatically inside the TikTok download pipeline.

The Kling generation step should only use videos where:

```text
solo_person_status == "solo"
```

## Acceptance Criteria

- TikTok ingest saves a thumbnail for each newly downloaded MP4 when Apify provides a cover URL.
- Metadata records the local thumbnail path and source thumbnail URL.
- Missing or failed thumbnail downloads do not break MP4 download.
- Kling generation can be filtered to solo-only source videos once detection is added.

## Open Questions

- Should person-count detection happen immediately during ingest or as a separate batch review step?
- Which detector should be used for counting people in thumbnails?
- Should `unknown` videos be skipped by default or sent to manual review?
