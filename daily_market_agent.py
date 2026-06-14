#!/usr/bin/env python3
"""Daily agentic marketing pipeline for source -> Kling -> drafted variants."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from postiz_video_poster import DEFAULT_PLATFORMS, ScheduledPost, schedule_video_posts
from tiktok_ingest import (
    DEFAULT_INPUT_DIR,
    DEFAULT_METADATA_DIR,
    DEFAULT_PROFILE_INPUT,
    PROJECT_ROOT,
    SOURCE_VIDEO_LEDGER_PATH,
    SourceVideo,
    acquire_source_video,
    call_kling_motion_control,
    sanitize_filename_part,
    to_camel_handle,
    use_pre_seed_videos,
)


VIDEOS_DIR = PROJECT_ROOT / "videos"
AI_BASE_DIR = VIDEOS_DIR / "ai" / "base"
CAPTIONED_DIR = VIDEOS_DIR / "drafted" / "captioned"
POSTED_DIR = VIDEOS_DIR / "posted"
KLING_MOTION_TRIM_SECONDS = 15
CAPTIONS_PATH = PROJECT_ROOT / "captions.txt"
STELLA_CHARACTER_PATH = PROJECT_ROOT / "characters" / "stella.JPG"
STELLA_CHARACTER_HANDLE = "stella"
POSTS_LEDGER_PATH = DEFAULT_METADATA_DIR / "posts.jsonl"
PIPELINE_LEDGER_PATH = DEFAULT_METADATA_DIR / "video_pipeline.csv"
PIPELINE_COLUMNS = [
    "source_video_id",
    "ai_video_id",
    "ai_stage_status",
    "ai_task_id",
    "ai_output_path",
    "output_video_id",
    "variant_number",
    "caption_hash",
    "caption_stage_status",
    "captioned_output_path",
    "post_stage_status",
    "last_error",
    "updated_at_utc",
]


@dataclass(frozen=True)
class DraftedVideo:
    output_video_id: str
    variant_number: int
    caption: str
    caption_slug: str
    caption_hash: str
    drafted_path: Path
    metadata_path: Path


@dataclass(frozen=True)
class CaptionVariantSpec:
    output_video_id: str
    variant_number: int
    caption: str
    caption_slug: str
    caption_hash: str


@dataclass(frozen=True)
class DailyAgentRun:
    run_id: str
    source_video: SourceVideo
    character_handle: str
    kling_output_path: Path
    drafted_videos: list[DraftedVideo]
    submitted_paths: list[Path]


def run_daily_agent(
    *,
    source_mode: str = "apify",
    profile_input: str = DEFAULT_PROFILE_INPUT,
    pre_seed_paths: list[str | Path] | None = None,
    source_video_id: str = "",
    run_date: datetime | None = None,
    variants: int = 3,
    stage_submitted: bool = False,
    post_platforms: list[str] | None = None,
    post_schedule_date: str | None = None,
    post_title: str = "",
    post_dry_run: bool = False,
) -> DailyAgentRun:
    current_run_date = run_date or datetime.now(timezone.utc)
    source_video = acquire_agent_source_video(
        source_mode=source_mode,
        profile_input=profile_input,
        pre_seed_paths=pre_seed_paths,
        source_video_id=source_video_id,
        run_date=current_run_date,
    )
    captions = choose_caption_variants(CAPTIONS_PATH, variants)
    ai_video_id = make_ai_video_id(source_video.source_video_id, STELLA_CHARACTER_HANDLE, current_run_date)
    variant_specs = build_caption_variant_specs(source_video, STELLA_CHARACTER_HANDLE, captions)
    output_video_ids = [spec.output_video_id for spec in variant_specs]
    prepare_pipeline_rows(source_video, ai_video_id, variant_specs)

    try:
        kling_output_path = generate_kling_base_video(source_video, ai_video_id, output_video_ids)
        mark_pipeline_stage(
            output_video_ids,
            ai_stage_status="finished",
            ai_output_path=str(kling_output_path),
            last_error="",
        )
    except Exception as exc:
        mark_pipeline_stage(output_video_ids, ai_stage_status="failed", last_error=short_error(exc))
        raise

    try:
        drafted_videos = draft_caption_variants(kling_output_path, source_video, ai_video_id, variant_specs)
    except Exception:
        raise

    submitted_paths = post_drafted_videos(
        drafted_videos,
        platforms=post_platforms or ["tiktok"],
        schedule_date=post_schedule_date,
        title=post_title,
        dry_run=post_dry_run,
    ) if stage_submitted else []

    run = DailyAgentRun(
        run_id=ai_video_id,
        source_video=source_video,
        character_handle=STELLA_CHARACTER_HANDLE,
        kling_output_path=kling_output_path,
        drafted_videos=drafted_videos,
        submitted_paths=submitted_paths,
    )
    append_posts_ledger(run)
    return run


def acquire_agent_source_video(
    *,
    source_mode: str,
    profile_input: str,
    pre_seed_paths: list[str | Path] | None,
    source_video_id: str,
    run_date: datetime,
) -> SourceVideo:
    if source_video_id:
        return load_source_video_from_ledger(source_video_id)
    if source_mode == "apify":
        return acquire_source_video(profile_input=profile_input, run_date=run_date)
    if source_mode == "preseed":
        if pre_seed_paths:
            candidates = [Path(path) for path in pre_seed_paths]
        else:
            candidates = sorted(DEFAULT_INPUT_DIR.glob("*.mp4"))
            if not candidates:
                candidates = sorted((VIDEOS_DIR / "input").glob("*.mp4"))
        for candidate in candidates:
            source_videos = use_pre_seed_videos(
                pre_seed_paths=[candidate],
                source_handle="preSeed",
                run_date=run_date,
                input_dir=DEFAULT_INPUT_DIR,
                source_ledger_path=SOURCE_VIDEO_LEDGER_PATH,
            )
            if source_videos:
                return source_videos[0]
        raise RuntimeError("No unused pre-seed source videos are available")
    raise ValueError("source_mode must be 'apify' or 'preseed'")


def load_source_video_from_ledger(source_video_id: str) -> SourceVideo:
    if not SOURCE_VIDEO_LEDGER_PATH.exists():
        raise FileNotFoundError(f"Source ledger does not exist: {SOURCE_VIDEO_LEDGER_PATH}")

    with SOURCE_VIDEO_LEDGER_PATH.open("r", encoding="utf-8") as ledger_file:
        for line in ledger_file:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("source_video_id") or "") != source_video_id:
                continue

            input_path = Path(str(row.get("input_path") or ""))
            if not input_path.exists():
                raise FileNotFoundError(f"Tracked source video file does not exist: {input_path}")

            return SourceVideo(
                source_video_id=source_video_id,
                source_handle=str(row.get("source_handle") or "unknown"),
                posted_at=datetime.fromisoformat(str(row["posted_at_utc"]).replace("Z", "+00:00")).astimezone(timezone.utc),
                video_url=str(row.get("video_url") or ""),
                input_path=input_path,
                source_url=str(row.get("source_url") or ""),
                download_url=str(row.get("download_url") or ""),
                source_kind=str(row.get("source_kind") or "apify_seed"),
                raw=row.get("raw") if isinstance(row.get("raw"), dict) else row,
            )

    raise ValueError(f"Source video id not found in source ledger: {source_video_id}")


def generate_kling_base_video(source_video: SourceVideo, ai_video_id: str, output_video_ids: list[str]) -> Path:
    output_path = AI_BASE_DIR / f"{ai_video_id}.mp4"
    if output_path.exists():
        return output_path

    def mark_ai_started(task_id: str) -> None:
        mark_pipeline_stage(
            output_video_ids,
            ai_stage_status="started",
            ai_task_id=task_id,
            last_error="",
        )

    with kling_motion_reference(source_video) as motion_video_input:
        result = call_kling_motion_control(
            source_video_input=STELLA_CHARACTER_PATH,
            motion_video_input=motion_video_input,
            prompt="Create a clean vertical TikTok-style marketing video with Stella following the reference motion.",
            character_orientation="video",
            aspect_ratio="9:16",
            output_path=output_path,
            on_task_started=mark_ai_started,
        )
    return result.output_path


class kling_motion_reference:
    def __init__(self, source_video: SourceVideo) -> None:
        self.source_video = source_video
        self.temp_dir: Path | None = None
        self.motion_path: Path | None = None

    def __enter__(self) -> str | Path:
        duration = probe_video_duration(self.source_video.input_path)
        if duration <= KLING_MOTION_TRIM_SECONDS:
            return self.source_video.video_url

        self.temp_dir = Path(tempfile.mkdtemp(prefix=f"kling-motion-{self.source_video.source_video_id}-"))
        self.motion_path = self.temp_dir / f"{self.source_video.source_video_id}_15s.mp4"
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(self.source_video.input_path),
                "-t",
                str(KLING_MOTION_TRIM_SECONDS),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(self.motion_path),
            ]
        )
        return self.motion_path

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)


def draft_caption_variants(
    kling_output_path: Path,
    source_video: SourceVideo,
    ai_video_id: str,
    variant_specs: list[CaptionVariantSpec],
) -> list[DraftedVideo]:
    drafted_videos: list[DraftedVideo] = []
    for spec in variant_specs:
        drafted_path = CAPTIONED_DIR / f"{spec.output_video_id}.mp4"
        metadata_path = drafted_path.with_suffix(".json")
        try:
            if not drafted_path.exists():
                burn_caption_into_video(kling_output_path, spec.caption, drafted_path)
            write_drafted_metadata(metadata_path, source_video, ai_video_id, kling_output_path, drafted_path, spec)
            mark_pipeline_stage(
                [spec.output_video_id],
                caption_stage_status="finished",
                captioned_output_path=str(drafted_path),
                last_error="",
            )
        except Exception as exc:
            mark_pipeline_stage(
                [spec.output_video_id],
                caption_stage_status="failed",
                last_error=short_error(exc),
            )
            raise
        drafted_videos.append(
            DraftedVideo(
                output_video_id=spec.output_video_id,
                variant_number=spec.variant_number,
                caption=spec.caption,
                caption_slug=spec.caption_slug,
                caption_hash=spec.caption_hash,
                drafted_path=drafted_path,
                metadata_path=metadata_path,
            )
        )
    return drafted_videos


def burn_caption_into_video(source_path: Path, caption: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = probe_video_dimensions(source_path)
    safe_box_width = int(width * 0.89)
    safe_box_height = int(height * 0.195)
    box_x = (width - safe_box_width) // 2
    box_y = int(height * 0.615)
    text_x = box_x + int(width * 0.04)
    text_y = box_y + int(height * 0.02)
    text_width = safe_box_width - int(width * 0.08)
    text_height = safe_box_height - int(height * 0.04)
    point_size = max(38, int(width * 0.075))

    with tempfile_assets(output_path.stem) as assets:
        caption_text_path = assets / "caption.txt"
        caption_text_path.write_text(caption, encoding="utf-8")
        text_png = assets / "caption_text.png"
        overlay_png = assets / "caption_overlay.png"

        run_command(
            [
                "magick",
                "-background",
                "none",
                "-fill",
                "white",
                "-font",
                "Arial-Bold",
                "-pointsize",
                str(point_size),
                "-gravity",
                "center",
                "-size",
                f"{text_width}x{text_height}",
                f"caption:@{caption_text_path}",
                str(text_png),
            ]
        )
        run_command(
            [
                "magick",
                "-size",
                f"{width}x{height}",
                "xc:none",
                "(",
                "-size",
                f"{safe_box_width}x{safe_box_height}",
                "xc:none",
                "-fill",
                "#00000099",
                "-draw",
                f"roundrectangle 0,0 {safe_box_width - 1},{safe_box_height - 1} 34,34",
                ")",
                "-geometry",
                f"+{box_x}+{box_y}",
                "-composite",
                str(text_png),
                "-geometry",
                f"+{text_x}+{text_y}",
                "-composite",
                str(overlay_png),
            ]
        )
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source_path),
                "-i",
                str(overlay_png),
                "-filter_complex",
                "[0:v][1:v]overlay=0:0:format=auto[v]",
                "-map",
                "[v]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )


def post_drafted_videos(
    drafted_videos: list[DraftedVideo],
    *,
    platforms: list[str],
    schedule_date: str | None,
    title: str,
    dry_run: bool,
) -> list[Path]:
    POSTED_DIR.mkdir(parents=True, exist_ok=True)
    posted_paths: list[Path] = []
    for draft in drafted_videos:
        submitted_path = POSTED_DIR / draft.drafted_path.name
        try:
            post_results = schedule_video_posts(
                draft.drafted_path,
                draft.caption,
                schedule_date=schedule_date,
                platforms=platforms,
                title=title,
                tags=[],
                max_posts=1,
                dry_run=dry_run,
            )
            write_post_metadata(draft, post_results, platforms, schedule_date, dry_run)
            if dry_run:
                continue
            if not submitted_path.exists():
                shutil.copy2(draft.drafted_path, submitted_path)
            mark_pipeline_stage([draft.output_video_id], post_stage_status="posted", last_error="")
        except Exception as exc:
            mark_pipeline_stage(
                [draft.output_video_id],
                post_stage_status="failed",
                last_error=short_error(exc),
            )
            raise
        posted_paths.append(submitted_path)
    return posted_paths


def stage_submitted_videos(drafted_videos: list[DraftedVideo]) -> list[Path]:
    return post_drafted_videos(
        drafted_videos,
        platforms=["tiktok"],
        schedule_date=None,
        title="",
        dry_run=False,
    )


def write_post_metadata(
    draft: DraftedVideo,
    post_results: list[ScheduledPost] | list[dict[str, object]],
    platforms: list[str],
    schedule_date: str | None,
    dry_run: bool,
) -> None:
    metadata_path = POSTED_DIR / f"{draft.output_video_id}.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "output_video_id": draft.output_video_id,
        "drafted_path": str(draft.drafted_path),
        "caption": draft.caption,
        "caption_hash": draft.caption_hash,
        "platforms": platforms,
        "schedule_date": schedule_date,
        "dry_run": dry_run,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "post_results": serialize_post_results(post_results),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def serialize_post_results(post_results: list[ScheduledPost] | list[dict[str, object]]) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for result in post_results:
        if isinstance(result, ScheduledPost):
            serialized.append(asdict(result))
        else:
            serialized.append(result)
    return serialized


def choose_caption_variants(captions_path: Path = CAPTIONS_PATH, count: int = 3) -> list[str]:
    captions = [line.strip() for line in captions_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(captions) < count:
        raise ValueError(f"Need at least {count} captions in {captions_path}")
    return random.sample(captions, count)


def build_asset_base_name(run_date: datetime, source_video: SourceVideo, character_handle: str) -> str:
    run_date_part = run_date.astimezone(timezone.utc).strftime("%Y%m%d")
    return "_".join(
        [
            run_date_part,
            sanitize_filename_part(source_video.source_handle),
            sanitize_filename_part(source_video.source_video_id),
            sanitize_filename_part(to_camel_handle(character_handle)),
        ]
    )


def make_caption_slug(caption: str, max_words: int = 8) -> str:
    ascii_caption = caption.encode("ascii", errors="ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", ascii_caption.lower())[:max_words]
    return "_".join(words) or "caption"


def build_caption_variant_specs(
    source_video: SourceVideo,
    character_handle: str,
    captions: list[str],
) -> list[CaptionVariantSpec]:
    specs: list[CaptionVariantSpec] = []
    for index, caption in enumerate(captions, start=1):
        hashed_caption = caption_hash(caption)
        specs.append(
            CaptionVariantSpec(
                output_video_id=make_output_video_id(
                    source_video.source_video_id,
                    character_handle,
                    index,
                    hashed_caption,
                ),
                variant_number=index,
                caption=caption,
                caption_slug=make_caption_slug(caption),
                caption_hash=hashed_caption,
            )
        )
    return specs


def make_ai_video_id(source_video_id: str, character_handle: str, run_date: datetime) -> str:
    date_part = run_date.astimezone(timezone.utc).strftime("%Y%m%d")
    return "_".join(
        [
            "ai",
            sanitize_filename_part(source_video_id),
            sanitize_filename_part(to_camel_handle(character_handle)),
            date_part,
        ]
    )


def make_output_video_id(source_video_id: str, character_handle: str, variant_number: int, hashed_caption: str) -> str:
    return "_".join(
        [
            "out",
            sanitize_filename_part(source_video_id),
            sanitize_filename_part(to_camel_handle(character_handle)),
            f"v{variant_number:02d}",
            sanitize_filename_part(hashed_caption),
        ]
    )


def caption_hash(caption: str) -> str:
    return hashlib.sha256(caption.encode("utf-8")).hexdigest()[:8]


def prepare_pipeline_rows(
    source_video: SourceVideo,
    ai_video_id: str,
    variant_specs: list[CaptionVariantSpec],
) -> None:
    rows = read_pipeline_rows()
    for spec in variant_specs:
        row = rows.get(spec.output_video_id, {column: "" for column in PIPELINE_COLUMNS})
        row["source_video_id"] = source_video.source_video_id
        row["ai_video_id"] = ai_video_id
        row["output_video_id"] = spec.output_video_id
        row["variant_number"] = str(spec.variant_number)
        row["caption_hash"] = spec.caption_hash
        row["ai_stage_status"] = row.get("ai_stage_status") or "not_started"
        row["caption_stage_status"] = row.get("caption_stage_status") or "not_started"
        row["post_stage_status"] = row.get("post_stage_status") or "not_started"
        row["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        rows[spec.output_video_id] = row
    write_pipeline_rows(rows)


def upsert_pipeline_row(row_update: dict[str, str]) -> None:
    rows = read_pipeline_rows()
    output_video_id = row_update["output_video_id"]
    now = datetime.now(timezone.utc).isoformat()
    existing = rows.get(output_video_id, {column: "" for column in PIPELINE_COLUMNS})
    existing.update(row_update)
    existing["updated_at_utc"] = now
    rows[output_video_id] = existing
    write_pipeline_rows(rows)


def mark_pipeline_stage(output_video_ids: list[str], **updates: str) -> None:
    rows = read_pipeline_rows()
    now = datetime.now(timezone.utc).isoformat()
    for output_video_id in output_video_ids:
        row = rows.get(output_video_id)
        if row is None:
            row = {column: "" for column in PIPELINE_COLUMNS}
            row["output_video_id"] = output_video_id
        row.update({key: value for key, value in updates.items() if key in PIPELINE_COLUMNS})
        row["updated_at_utc"] = now
        rows[output_video_id] = row
    write_pipeline_rows(rows)


def read_pipeline_rows() -> dict[str, dict[str, str]]:
    if not PIPELINE_LEDGER_PATH.exists():
        return {}

    with PIPELINE_LEDGER_PATH.open("r", newline="", encoding="utf-8") as ledger_file:
        rows: dict[str, dict[str, str]] = {}
        for row in csv.DictReader(ledger_file):
            output_video_id = row.get("output_video_id")
            if output_video_id:
                rows[output_video_id] = {column: row.get(column, "") for column in PIPELINE_COLUMNS}
        return rows


def write_pipeline_rows(rows: dict[str, dict[str, str]]) -> None:
    PIPELINE_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PIPELINE_LEDGER_PATH.open("w", newline="", encoding="utf-8") as ledger_file:
        writer = csv.DictWriter(ledger_file, fieldnames=PIPELINE_COLUMNS)
        writer.writeheader()
        for output_video_id in sorted(rows):
            writer.writerow({column: rows[output_video_id].get(column, "") for column in PIPELINE_COLUMNS})


def short_error(exc: Exception, max_length: int = 500) -> str:
    message = f"{type(exc).__name__}: {exc}"
    return message[:max_length]


def write_drafted_metadata(
    metadata_path: Path,
    source_video: SourceVideo,
    ai_video_id: str,
    kling_output_path: Path,
    drafted_path: Path,
    spec: CaptionVariantSpec,
) -> None:
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "source_video_id": source_video.source_video_id,
        "source_handle": source_video.source_handle,
        "source_video_url": source_video.video_url,
        "source_input_path": str(source_video.input_path),
        "ai_video_id": ai_video_id,
        "output_video_id": spec.output_video_id,
        "character_handle": STELLA_CHARACTER_HANDLE,
        "character_path": str(STELLA_CHARACTER_PATH),
        "kling_output_path": str(kling_output_path),
        "drafted_path": str(drafted_path),
        "variant_number": spec.variant_number,
        "caption": spec.caption,
        "caption_slug": spec.caption_slug,
        "caption_hash": spec.caption_hash,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def append_posts_ledger(run: DailyAgentRun) -> None:
    POSTS_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    posted_path_names = {path.name for path in run.submitted_paths}
    with POSTS_LEDGER_PATH.open("a", encoding="utf-8") as ledger_file:
        for draft in run.drafted_videos:
            row = {
                "run_id": run.run_id,
                "source_video_id": run.source_video.source_video_id,
                "source_handle": run.source_video.source_handle,
                "character_handle": run.character_handle,
                "kling_output_path": str(run.kling_output_path),
                "output_video_id": draft.output_video_id,
                "drafted_path": str(draft.drafted_path),
                "variant_number": draft.variant_number,
                "caption": draft.caption,
                "caption_slug": draft.caption_slug,
                "caption_hash": draft.caption_hash,
                "status": "posted" if draft.drafted_path.name in posted_path_names else "drafted",
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            ledger_file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def probe_video_dimensions(video_path: Path) -> tuple[int, int]:
    completed = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(video_path),
        ],
        capture_output=True,
    )
    width, height = completed.stdout.strip().split("x", 1)
    return int(width), int(height)


def probe_video_duration(video_path: Path) -> float:
    completed = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
    )
    return float(completed.stdout.strip())


def run_command(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=capture_output)


class tempfile_assets:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.path: Path | None = None

    def __enter__(self) -> Path:
        import tempfile

        self.path = Path(tempfile.mkdtemp(prefix=f"{self.prefix}-"))
        return self.path

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.path and self.path.exists():
            shutil.rmtree(self.path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the daily content marketing agent.")
    parser.add_argument("--source-mode", choices=("apify", "preseed"), default="apify")
    parser.add_argument("--profile", default=DEFAULT_PROFILE_INPUT)
    parser.add_argument("--pre-seed", nargs="*", default=None, help="Optional local MP4 paths for pre-seed mode")
    parser.add_argument("--source-video-id", default="", help="Use a seeded source from videos/metadata/source_videos.jsonl by original TikTok id.")
    parser.add_argument("--variants", type=int, default=3)
    parser.add_argument("--post", dest="stage_submitted", action="store_true", help="Post/schedule generated drafts through Postiz.")
    parser.add_argument("--stage-submitted", dest="stage_submitted", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--post-platform",
        action="append",
        choices=DEFAULT_PLATFORMS,
        help="Platform to post to through Postiz. Repeat for multiple platforms. Defaults to TikTok.",
    )
    parser.add_argument("--post-date", help="Eastern calendar date to schedule in YYYY-MM-DD format. Defaults to next available slot.")
    parser.add_argument("--post-title", default="", help="Optional platform title. Defaults to caption behavior in Postiz helper.")
    parser.add_argument("--post-dry-run", action="store_true", help="Build Postiz payloads without uploading or scheduling.")
    args = parser.parse_args()

    run = run_daily_agent(
        source_mode=args.source_mode,
        profile_input=args.profile,
        pre_seed_paths=args.pre_seed,
        source_video_id=args.source_video_id,
        variants=args.variants,
        stage_submitted=args.stage_submitted,
        post_platforms=args.post_platform,
        post_schedule_date=args.post_date,
        post_title=args.post_title,
        post_dry_run=args.post_dry_run,
    )
    print(f"run_id: {run.run_id}")
    print(f"kling_output: {run.kling_output_path}")
    for draft in run.drafted_videos:
        print(f"drafted_v{draft.variant_number:02d}: {draft.drafted_path}")


if __name__ == "__main__":
    main()
