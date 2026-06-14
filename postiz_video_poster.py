#!/usr/bin/env python3
"""Schedule local MP4 videos through the Postiz public API."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_POSTIZ_API_BASE_URL = "https://api.postiz.com/public/v1"
EASTERN_TIME = ZoneInfo("America/New_York")
DEFAULT_PLATFORMS = ("tiktok", "instagram", "facebook")
DEFAULT_SCHEDULE_TIMES = (time(12, 0), time(17, 0), time(20, 0))
SUPPORTED_VIDEO_MIME_TYPES = {"video/mp4"}


@dataclass(frozen=True)
class UploadedMedia:
    id: str
    path: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ScheduledPost:
    scheduled_at_utc: datetime
    platforms: tuple[str, ...]
    response: dict[str, Any] | list[Any]


class PostizClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("POSTIZ_API_KEY") or load_dotenv_value(PROJECT_ROOT / ".env", "POSTIZ_API_KEY")
        self.base_url = (base_url or os.environ.get("POSTIZ_API_BASE_URL") or DEFAULT_POSTIZ_API_BASE_URL).rstrip("/")
        if not self.api_key:
            raise RuntimeError("Set POSTIZ_API_KEY before scheduling posts through Postiz")

    def list_integrations(self) -> list[dict[str, Any]]:
        payload = self._request_json("GET", "/integrations")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        integrations = payload.get("integrations") if isinstance(payload, dict) else None
        if isinstance(integrations, list):
            return [item for item in integrations if isinstance(item, dict)]
        raise RuntimeError(f"Expected Postiz integrations list, got {type(payload).__name__}")

    def upload_file(self, video_path: Path) -> UploadedMedia:
        boundary = f"----postiz-video-poster-{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(video_path.name)[0] or "application/octet-stream"
        body = build_multipart_file_body(boundary, "file", video_path, content_type)
        payload = self._request_json(
            "POST",
            "/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"Expected Postiz upload response object, got {type(payload).__name__}")

        media_id = str(payload.get("id") or "")
        media_path = str(payload.get("path") or "")
        if not media_id or not media_path:
            raise RuntimeError(f"Postiz upload response did not include id and path: {payload}")
        return UploadedMedia(id=media_id, path=media_path, raw=payload)

    def create_post(self, payload: dict[str, Any]) -> dict[str, Any] | list[Any]:
        response = self._request_json(
            "POST",
            "/posts",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(response, (dict, list)):
            raise RuntimeError(f"Expected Postiz create-post response object or list, got {type(response).__name__}")
        return response

    def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        request_headers = {"Authorization": self.api_key}
        request_headers.update(headers or {})
        request = Request(f"{self.base_url}{endpoint}", data=data, headers=request_headers, method=method)

        try:
            with urlopen(request, timeout=180) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Postiz request failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Postiz request failed: {exc.reason}") from exc

        if not response_body:
            return {}
        return json.loads(response_body)


def schedule_video_posts(
    video_path: str | Path,
    caption: str,
    *,
    schedule_date: date | str | None = None,
    platforms: tuple[str, ...] | list[str] = DEFAULT_PLATFORMS,
    integration_ids: dict[str, str] | None = None,
    title: str = "",
    tags: list[str] | None = None,
    max_posts: int | None = None,
    dry_run: bool = False,
    client: PostizClient | None = None,
) -> list[ScheduledPost] | list[dict[str, Any]]:
    """Upload an MP4 and schedule it at 12 PM, 5 PM, and 8 PM Eastern.

    The orchestrator can pass explicit integration IDs, or set environment
    variables such as POSTIZ_TIKTOK_INTEGRATION_ID. If neither is present,
    integrations are discovered from Postiz and matched by platform name.
    """
    resolved_video_path = validate_video_path(video_path)
    normalized_platforms = normalize_platforms(platforms)
    scheduled_times = build_eastern_schedule(schedule_date)
    if max_posts is not None:
        if max_posts < 1:
            raise ValueError("max_posts must be at least 1")
        scheduled_times = scheduled_times[:max_posts]
    postiz = client or (None if dry_run else PostizClient())

    media = UploadedMedia(id="dry-run-media-id", path=str(resolved_video_path), raw={})
    if not dry_run:
        media = postiz.upload_file(resolved_video_path)

    resolved_integration_ids = resolve_integration_ids(postiz, normalized_platforms, integration_ids or {}, dry_run=dry_run)
    payloads = [
        build_post_payload(
            media,
            caption=caption,
            scheduled_at=scheduled_at,
            platforms=normalized_platforms,
            integration_ids=resolved_integration_ids,
            title=title,
            tags=tags or [],
        )
        for scheduled_at in scheduled_times
    ]

    if dry_run:
        return payloads

    scheduled_posts: list[ScheduledPost] = []
    for payload in payloads:
        response = postiz.create_post(payload)
        scheduled_posts.append(
            ScheduledPost(
                scheduled_at_utc=parse_utc_datetime(str(payload["date"])),
                platforms=tuple(normalized_platforms),
                response=response,
            )
        )
    return scheduled_posts


def build_post_payload(
    media: UploadedMedia,
    *,
    caption: str,
    scheduled_at: datetime,
    platforms: list[str],
    integration_ids: dict[str, str],
    title: str,
    tags: list[str],
) -> dict[str, Any]:
    media_ref = {"id": media.id, "path": media.path}
    return {
        "type": "schedule",
        "date": scheduled_at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "shortLink": False,
        "tags": tags,
        "posts": [
            {
                "integration": {"id": integration_ids[platform]},
                "value": [{"content": caption, "image": [media_ref]}],
                "settings": platform_settings(platform, caption, title),
            }
            for platform in platforms
        ],
    }


def platform_settings(platform: str, caption: str, title: str) -> dict[str, Any]:
    if platform == "tiktok":
        return {
            "__type": "tiktok",
            "title": (title or caption).strip()[:90],
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "duet": True,
            "stitch": True,
            "comment": True,
            "autoAddMusic": "no",
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "video_made_with_ai": False,
            "content_posting_method": "DIRECT_POST",
        }
    if platform == "instagram":
        return {
            "__type": "instagram",
            "post_type": "post",
            "is_trial_reel": False,
            "collaborators": [],
        }
    if platform == "facebook":
        return {"__type": "facebook"}
    raise ValueError(f"Unsupported platform: {platform}")


def build_eastern_schedule(schedule_date: date | str | None = None, now: datetime | None = None) -> list[datetime]:
    current_time = now.astimezone(EASTERN_TIME) if now else datetime.now(EASTERN_TIME)
    target_date = parse_schedule_date(schedule_date) if schedule_date else current_time.date()
    scheduled_times = [datetime.combine(target_date, slot, tzinfo=EASTERN_TIME) for slot in DEFAULT_SCHEDULE_TIMES]

    if schedule_date is None:
        scheduled_times = [scheduled_at for scheduled_at in scheduled_times if scheduled_at > current_time]
        if not scheduled_times:
            tomorrow = current_time.date().fromordinal(current_time.date().toordinal() + 1)
            scheduled_times = [datetime.combine(tomorrow, slot, tzinfo=EASTERN_TIME) for slot in DEFAULT_SCHEDULE_TIMES]

    if any(scheduled_at <= current_time for scheduled_at in scheduled_times):
        raise ValueError("All scheduled times must be in the future")
    return [scheduled_at.astimezone(timezone.utc) for scheduled_at in scheduled_times]


def resolve_integration_ids(
    client: PostizClient | None,
    platforms: list[str],
    explicit_ids: dict[str, str],
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    resolved = {platform: explicit_ids[platform] for platform in platforms if explicit_ids.get(platform)}
    for platform in platforms:
        env_value = os.environ.get(f"POSTIZ_{platform.upper()}_INTEGRATION_ID") or load_dotenv_value(
            PROJECT_ROOT / ".env",
            f"POSTIZ_{platform.upper()}_INTEGRATION_ID",
        )
        if env_value:
            resolved[platform] = env_value

    missing_platforms = [platform for platform in platforms if platform not in resolved]
    if not missing_platforms:
        return resolved
    if dry_run:
        for platform in missing_platforms:
            resolved[platform] = f"dry-run-{platform}-integration-id"
        return resolved
    if client is None:
        raise RuntimeError("Postiz client is required when dry_run is false")

    integrations = client.list_integrations()
    for platform in missing_platforms:
        match = find_integration_for_platform(integrations, platform)
        if not match:
            raise RuntimeError(
                f"No Postiz integration found for {platform}. "
                f"Set POSTIZ_{platform.upper()}_INTEGRATION_ID or connect that channel in Postiz."
            )
        resolved[platform] = str(match["id"])
    return resolved


def find_integration_for_platform(integrations: list[dict[str, Any]], platform: str) -> dict[str, Any] | None:
    aliases = {
        "tiktok": {"tiktok"},
        "instagram": {"instagram", "instagram-standalone"},
        "facebook": {"facebook", "facebook-page"},
    }[platform]
    for integration in integrations:
        if integration.get("disabled") is True:
            continue
        values = {
            str(integration.get("identifier") or "").lower(),
            str(integration.get("providerIdentifier") or "").lower(),
            str(integration.get("provider") or "").lower(),
            str(integration.get("name") or "").lower(),
        }
        if values & aliases:
            return integration
    return None


def validate_video_path(video_path: str | Path) -> Path:
    path = Path(video_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Video file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Video path is not a file: {path}")

    mime_type = mimetypes.guess_type(path.name)[0] or ""
    if mime_type not in SUPPORTED_VIDEO_MIME_TYPES:
        raise ValueError("Postiz upload currently accepts MP4 video files; provide a .mp4 file")
    return path


def normalize_platforms(platforms: tuple[str, ...] | list[str]) -> list[str]:
    normalized: list[str] = []
    for platform in platforms:
        value = platform.strip().lower()
        if value not in DEFAULT_PLATFORMS:
            raise ValueError(f"Unsupported platform: {platform}")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise ValueError("At least one platform is required")
    return normalized


def build_multipart_file_body(boundary: str, field_name: str, file_path: Path, content_type: str) -> bytes:
    lines = [
        f"--{boundary}",
        f'Content-Disposition: form-data; name="{field_name}"; filename="{file_path.name}"',
        f"Content-Type: {content_type}",
        "",
    ]
    return (
        "\r\n".join(lines).encode("utf-8")
        + b"\r\n"
        + file_path.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )


def parse_schedule_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def parse_utc_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_dotenv_value(env_path: Path, key: str) -> str:
    if not env_path.exists():
        return ""

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        name, value = stripped.split("=", 1)
        if name.strip() == key:
            return value.strip().strip("\"'")
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Schedule an MP4 video through Postiz.")
    parser.add_argument("video_path", help="Local .mp4 path to upload and schedule.")
    parser.add_argument("--caption", required=True, help="Caption/content for each scheduled post.")
    parser.add_argument("--date", help="Eastern calendar date to schedule, in YYYY-MM-DD format. Defaults to next available slots.")
    parser.add_argument("--title", default="", help="Optional TikTok title. Defaults to the caption truncated to 90 characters.")
    parser.add_argument(
        "--platform",
        action="append",
        choices=DEFAULT_PLATFORMS,
        help="Platform to post to. Repeat for multiple platforms. Defaults to TikTok, Instagram, and Facebook.",
    )
    parser.add_argument("--tag", action="append", default=[], help="Optional Postiz tag. Repeat for multiple tags.")
    parser.add_argument("--max-posts", type=int, help="Limit how many schedule slots to create.")
    parser.add_argument("--dry-run", action="store_true", help="Print payloads without uploading or scheduling.")
    args = parser.parse_args()

    results = schedule_video_posts(
        args.video_path,
        args.caption,
        schedule_date=args.date,
        platforms=args.platform or list(DEFAULT_PLATFORMS),
        title=args.title,
        tags=args.tag,
        max_posts=args.max_posts,
        dry_run=args.dry_run,
    )
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
