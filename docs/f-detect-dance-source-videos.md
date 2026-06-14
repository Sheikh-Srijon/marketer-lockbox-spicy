# Feature Idea: Detect Dance Source Videos

## Goal

Only send source videos into Kling when they are likely dance videos. Some downloaded TikToks are edits, templates, or low-motion clips, which are poor motion-control references.

## Pipeline Placement

Add a source classification step before AI generation:

```text
source_acquired -> source_classified -> ai_video_started
```

Kling should only consume videos classified as likely dance sources.

## V1 Approach

Start with a lightweight metadata classifier using Apify metadata:

- Caption text
- Hashtags
- Music title
- Raw metadata fields

Positive signals:

- `dance`
- `choreo`
- `routine`
- `moves`
- `trend`
- `dancing`

Negative signals:

- `edit`
- `capcut`
- `template`
- `transition`
- `velocity`
- `slideshow`
- `photo`

This should produce a confidence score, not a hard truth.

## V2 Approach

Add visual motion analysis:

- Sample frames every 0.5 seconds.
- Detect whether a person is visible across most frames.
- Estimate body pose/keypoints with MediaPipe, YOLO-pose, or a similar model.
- Score rhythmic full-body motion over time.
- Penalize low-motion, slideshow-like, or mostly text/edit videos.

## Metadata Fields

Add source-level fields later:

```text
dance_candidate_status
dance_confidence
dance_reason
```

Possible statuses:

```text
dance
not_dance
unknown
error
```

Example:

```text
dance,0.82,"full-body person visible; rhythmic pose movement; caption contains dance"
not_dance,0.21,"caption contains edit/template; low human pose motion"
```

## Acceptance Criteria

- Downloaded source videos can be marked as `dance`, `not_dance`, `unknown`, or `error`.
- Non-dance sources are skipped before Kling generation.
- The classifier stores a short reason for manual review.
- V1 can run without heavy CV dependencies.
- V2 improves accuracy using sampled frames and pose/motion detection.
