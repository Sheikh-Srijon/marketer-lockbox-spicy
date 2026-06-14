#!/usr/bin/env python3
"""Download recent TikTok profile videos through Apify."""

from __future__ import annotations

import csv
import argparse
import base64
import json
import hashlib
import hmac
import mimetypes
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


APIFY_ACTOR_ID = "clockworks~tiktok-scraper"
APIFY_API_BASE_URL = "https://api.apify.com/v2"
KLING_API_BASE_URL = "https://api-singapore.klingai.com/v1"
KLING_MOTION_CONTROL_ENDPOINT = "/videos/motion-control"
DEFAULT_PROFILE_INPUT = "https://www.tiktok.com/@beatswith_harnidh"
DEFAULT_VIDEOS_DIR = Path(__file__).resolve().parent / "videos"
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CHARACTER_IMAGE_INPUT = PROJECT_ROOT / "characters" / "jessica.JPG"
METADATA_COLUMNS = [
    "video_id",
    "handle",
    "posted_at_utc",
    "mp4_path",
    "caption",
    "hashtags",
    "music_id",
    "music_title",
    "music_author",
    "music_original",
    "source_url",
    "download_url",
    "downloaded_at_utc",
    "raw_metadata_json",
]


@dataclass(frozen=True)
class TikTokVideo:
    video_id: str
    handle: str
    posted_at: datetime
    caption: str
    hashtags: list[str]
    music_id: str
    music_title: str
    music_author: str
    music_original: bool | None
    source_url: str
    download_url: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class KlingMotionControlResult:
    request_id: str
    output_video_url: str
    output_path: Path
    metadata_path: Path
    raw: dict[str, Any]


def parse_profile_handle(profile_input: str) -> str:
    """Accept a handle, @handle, or TikTok profile URL and return the handle."""
    value = profile_input.strip()
    if not value:
        raise ValueError("profile_input cannot be empty")

    match = re.search(r"tiktok\.com/@([^/?#\s]+)", value)
    if match:
        return match.group(1)

    return value.removeprefix("@").strip()


def build_actor_input(handle: str, results_per_page: int = 10) -> dict[str, Any]:
    return {
        "profiles": [handle],
        "profileScrapeSections": ["videos"],
        "profileSorting": "latest",
        "resultsPerPage": results_per_page,
        "excludePinnedPosts": False,
        "shouldDownloadVideos": True,
        "shouldDownloadCovers": False,
        "shouldDownloadMusicCovers": False,
        "shouldDownloadSubtitles": False,
        "shouldDownloadSlideshowImages": False,
        "scrapeRelatedVideos": False,
        "proxyCountryCode": "None",
    }


def fetch_profile_posts_from_apify(actor_input: dict[str, Any], token: str) -> list[dict[str, Any]]:
    params = urlencode({"token": token, "timeout": "300"})
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items?{params}"
    request = Request(
        url,
        data=json.dumps(actor_input).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=330) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Apify request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Apify request failed: {exc.reason}") from exc

    if not isinstance(payload, list):
        raise RuntimeError(f"Expected Apify dataset items list, got: {type(payload).__name__}")

    return [item for item in payload if isinstance(item, dict)]


def normalize_post(item: dict[str, Any], fallback_handle: str) -> TikTokVideo:
    video_id = str(_field(item, "id") or "")
    posted_at = _parse_apify_datetime(_field(item, "createTimeISO"), _field(item, "createTime"))
    hashtags = _extract_hashtags(_field(item, "hashtags") or [])

    return TikTokVideo(
        video_id=video_id,
        handle=str(_field(item, "authorMeta.name") or fallback_handle),
        posted_at=posted_at,
        caption=str(_field(item, "text") or ""),
        hashtags=hashtags,
        music_id=str(_field(item, "musicMeta.musicId") or ""),
        music_title=str(_field(item, "musicMeta.musicName") or ""),
        music_author=str(_field(item, "musicMeta.musicAuthor") or ""),
        music_original=_optional_bool(_field(item, "musicMeta.musicOriginal")),
        source_url=str(_field(item, "webVideoUrl") or ""),
        download_url=_extract_download_url(item),
        raw=item,
    )


def filter_recent_posts(posts: list[TikTokVideo], hours: int = 24, now: datetime | None = None) -> list[TikTokVideo]:
    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time - timedelta(hours=hours)
    return [post for post in posts if post.posted_at >= cutoff]


def load_seen_video_ids(metadata_path: Path) -> set[str]:
    if not metadata_path.exists():
        return set()

    with metadata_path.open("r", newline="", encoding="utf-8") as metadata_file:
        return {
            row["video_id"]
            for row in csv.DictReader(metadata_file)
            if row.get("video_id")
        }


def make_video_filename(video: TikTokVideo) -> str:
    posted = video.posted_at.astimezone(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    suffix = video.video_id or "unknown"
    return f"{posted}_{suffix}.mp4"


def download_mp4(download_url: str, output_path: Path, token: str = "") -> None:
    if not download_url:
        raise ValueError("No MP4 download URL was provided by Apify")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "privacy-vault-video-cloner/1.0"}
    if token and "api.apify.com/" in download_url:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(download_url, headers=headers)

    try:
        with urlopen(request, timeout=120) as response:
            with tempfile.NamedTemporaryFile(delete=False, dir=output_path.parent, suffix=".tmp") as tmp_file:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    tmp_file.write(chunk)
                tmp_path = Path(tmp_file.name)
    except Exception:
        if "tmp_path" in locals() and tmp_path.exists():
            tmp_path.unlink()
        raise

    tmp_path.replace(output_path)


def append_metadata(metadata_path: Path, video: TikTokVideo, mp4_path: Path) -> None:
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    should_write_header = not metadata_path.exists()
    row = {
        "video_id": video.video_id,
        "handle": video.handle,
        "posted_at_utc": video.posted_at.astimezone(timezone.utc).isoformat(),
        "mp4_path": str(mp4_path),
        "caption": video.caption,
        "hashtags": ",".join(video.hashtags),
        "music_id": video.music_id,
        "music_title": video.music_title,
        "music_author": video.music_author,
        "music_original": "" if video.music_original is None else str(video.music_original).lower(),
        "source_url": video.source_url,
        "download_url": video.download_url,
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_metadata_json": json.dumps(video.raw, ensure_ascii=False, sort_keys=True),
    }

    with metadata_path.open("a", newline="", encoding="utf-8") as metadata_file:
        writer = csv.DictWriter(metadata_file, fieldnames=METADATA_COLUMNS)
        if should_write_header:
            writer.writeheader()
        writer.writerow(row)


def call_kling_motion_control(
    source_video_input: str | Path = DEFAULT_CHARACTER_IMAGE_INPUT,
    motion_video_input: str | Path | None = None,
    *,
    prompt: str = "",
    character_orientation: str = "image",
    keep_original_sound: str = "yes",
    aspect_ratio: str = "9:16",
    mode: str = "standard",
    model_name: str = "kling-v2-6",
    poll_interval_seconds: int = 10,
    timeout_seconds: int = 900,
    output_dir: Path = DEFAULT_VIDEOS_DIR / "kling_outputs",
) -> KlingMotionControlResult:
    """Create a Kling motion-control video through the direct Kling developer API.

    Motion Control uses a character/source image plus a motion reference video.
    If source_video_input is a local video, the first frame is extracted with
    ffmpeg and sent as the source image. motion_video_input must be a publicly
    reachable URL because Kling's API expects video_url to be fetchable.
    """
    if motion_video_input is None:
        raise ValueError("motion_video_input is required")

    access_key = os.environ.get("KLING_ACCESS_KEY") or load_dotenv_value(PROJECT_ROOT / ".env", "KLING_ACCESS_KEY")
    secret_key = os.environ.get("KLING_SECRET_KEY") or load_dotenv_value(PROJECT_ROOT / ".env", "KLING_SECRET_KEY")
    if not access_key or not secret_key:
        raise RuntimeError("Set KLING_ACCESS_KEY and KLING_SECRET_KEY before running Kling motion control")
    if character_orientation not in {"image", "video"}:
        raise ValueError("character_orientation must be 'image' or 'video'")
    if keep_original_sound not in {"yes", "no"}:
        raise ValueError("keep_original_sound must be 'yes' or 'no'")
    if aspect_ratio not in {"16:9", "9:16", "1:1"}:
        raise ValueError("aspect_ratio must be '16:9', '9:16', or '1:1'")
    if mode not in {"std", "standard", "professional", "pro"}:
        raise ValueError("mode must be 'std', 'standard', 'professional', or 'pro'")

    source_image = resolve_kling_image_input(source_video_input)
    motion_url = resolve_kling_video_url(motion_video_input)
    kling_mode = "pro" if mode in {"professional", "pro"} else "std"
    payload: dict[str, Any] = {
        "model_name": model_name,
        "mode": kling_mode,
        "prompt": prompt,
        "character_orientation": character_orientation,
        "keep_original_sound": keep_original_sound,
        "aspect_ratio": aspect_ratio,
        "image_url": source_image,
        "video_url": motion_url,
    }
    token = generate_kling_jwt(access_key, secret_key)
    submit_result = post_json_to_kling(KLING_MOTION_CONTROL_ENDPOINT, payload, token)
    task_id = extract_kling_task_id(submit_result)
    if not task_id:
        raise RuntimeError(f"Kling motion control did not return a task_id: {submit_result}")
    print(f"Kling motion-control task started: {task_id}", flush=True)

    result_data = poll_kling_motion_control(task_id, token, poll_interval_seconds, timeout_seconds)
    output_video_url = extract_kling_video_url(result_data)
    if not output_video_url:
        raise RuntimeError(f"Kling motion control finished without a video URL: {result_data}")

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    output_path = output_dir / f"kling-motion-control-{timestamp}.mp4"
    metadata_path = output_path.with_suffix(".json")

    download_mp4(output_video_url, output_path)
    metadata = {
        "model_name": model_name,
        "mode": kling_mode,
        "task_id": task_id,
        "request_id": task_id,
        "source_video_input": str(source_video_input),
        "motion_video_input": str(motion_video_input),
        "source_image": source_image if is_public_url(source_image) else "[base64 image omitted]",
        "motion_url": motion_url,
        "prompt": prompt,
        "character_orientation": character_orientation,
        "keep_original_sound": keep_original_sound,
        "aspect_ratio": aspect_ratio,
        "output_video_url": output_video_url,
        "output_path": str(output_path),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "submit_result": submit_result,
        "raw_result": result_data,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    return KlingMotionControlResult(
        request_id=task_id,
        output_video_url=output_video_url,
        output_path=output_path,
        metadata_path=metadata_path,
        raw=result_data,
    )


def resolve_kling_image_input(file_input: str | Path) -> str:
    value = str(file_input)
    if is_public_url(value):
        return value

    path = resolve_local_path(Path(value).expanduser())
    if not path.exists():
        raise FileNotFoundError(f"Input file does not exist: {path}")

    image_path = path
    if path.suffix.lower() in {".mp4", ".mov", ".webm", ".m4v"}:
        image_path = extract_first_frame(path)

    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    if not mime_type.startswith("image/"):
        raise ValueError(f"Kling source input must be an image or video file, got: {path}")

    return base64.b64encode(image_path.read_bytes()).decode("ascii")


def resolve_kling_video_url(file_input: str | Path) -> str:
    value = str(file_input)
    if is_public_url(value):
        return add_apify_token_to_url(value)

    local_path = resolve_local_path(Path(value).expanduser())
    metadata_url = find_download_url_for_mp4(local_path)
    if metadata_url:
        return add_apify_token_to_url(metadata_url)

    if local_path.exists():
        return upload_file_to_apify_kv(local_path)

    raise ValueError(
        "Kling motion_video_input must be a publicly accessible URL. "
        "For local files, set APIFY_TOKEN so they can be uploaded to Apify storage first."
    )


def extract_first_frame(video_path: Path) -> Path:
    output_path = Path(tempfile.gettempdir()) / f"{video_path.stem}-kling-source-frame.png"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        str(output_path),
    ]
    import subprocess

    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to extract first frame from {video_path}: {completed.stderr}")

    return output_path


def resolve_local_path(path: Path) -> Path:
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    if path.exists() or not path.parent.exists():
        return path

    lowered_name = path.name.lower()
    for candidate in path.parent.iterdir():
        if candidate.name.lower() == lowered_name:
            return candidate

    return path


def is_public_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def find_download_url_for_mp4(mp4_path: Path) -> str:
    metadata_path = DEFAULT_VIDEOS_DIR / "metadata.csv"
    if not metadata_path.exists():
        return ""

    resolved_input = str(mp4_path.resolve()) if mp4_path.exists() else str(mp4_path)
    with metadata_path.open("r", newline="", encoding="utf-8") as metadata_file:
        for row in csv.DictReader(metadata_file):
            row_path = row.get("mp4_path", "")
            if row_path == resolved_input or Path(row_path).name == mp4_path.name:
                return row.get("download_url", "")

    return ""


def add_apify_token_to_url(url: str) -> str:
    if "api.apify.com/" not in url:
        return url

    token = os.environ.get("APIFY_TOKEN") or load_dotenv_value(PROJECT_ROOT / ".env", "APIFY_TOKEN")
    if not token:
        return url

    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("token", token)
    return urlunparse(parsed._replace(query=urlencode(query)))


def upload_file_to_apify_kv(file_path: Path) -> str:
    token = os.environ.get("APIFY_TOKEN") or load_dotenv_value(PROJECT_ROOT / ".env", "APIFY_TOKEN")
    if not token:
        raise RuntimeError("Set APIFY_TOKEN before uploading local files for Kling")

    store = create_apify_key_value_store(token)
    store_id = str(store.get("id") or _field(store, "data.id") or "")
    if not store_id:
        raise RuntimeError(f"Apify did not return a key-value store id: {store}")

    record_key = f"kling-motion-input-{int(time.time())}-{file_path.name}"
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    query = urlencode({"token": token})
    url = f"{APIFY_API_BASE_URL}/key-value-stores/{store_id}/records/{record_key}?{query}"
    request = Request(
        url,
        data=file_path.read_bytes(),
        headers={"Content-Type": content_type},
        method="PUT",
    )

    try:
        with urlopen(request, timeout=180) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Apify upload failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Apify upload failed: {exc.reason}") from exc

    return url


def create_apify_key_value_store(token: str) -> dict[str, Any]:
    query = urlencode({"token": token})
    request = Request(
        f"{APIFY_API_BASE_URL}/key-value-stores?{query}",
        data=json.dumps({"name": f"privacy-vault-kling-{int(time.time())}"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Apify key-value store creation failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Apify key-value store creation failed: {exc.reason}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected Apify key-value store response object, got {type(payload).__name__}")
    return payload


def generate_kling_jwt(access_key: str, secret_key: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "iss": access_key,
        "exp": now + 1800,
        "nbf": now - 5,
    }
    encoded_header = _base64url_json(header)
    encoded_payload = _base64url_json(payload)
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_base64url_bytes(signature)}"


def post_json_to_kling(endpoint: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    return request_json_from_kling(endpoint, token, method="POST", payload=payload)


def get_json_from_kling(endpoint: str, token: str) -> dict[str, Any]:
    return request_json_from_kling(endpoint, token, method="GET")


def request_json_from_kling(
    endpoint: str,
    token: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{KLING_API_BASE_URL}{endpoint}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )

    try:
        with urlopen(request, timeout=120) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Kling request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Kling request failed: {exc.reason}") from exc

    if not isinstance(response_data, dict):
        raise RuntimeError(f"Expected Kling JSON object, got {type(response_data).__name__}")
    return response_data


def poll_kling_motion_control(
    task_id: str,
    token: str,
    poll_interval_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    endpoint = f"{KLING_MOTION_CONTROL_ENDPOINT}/{task_id}"

    while time.time() < deadline:
        result = get_json_from_kling(endpoint, token)
        status = extract_kling_status(result)
        if status in {"succeed", "succeeded", "success", "completed", "complete"}:
            return result
        if status in {"failed", "failure", "error"}:
            raise RuntimeError(f"Kling motion control failed: {result}")
        time.sleep(poll_interval_seconds)

    raise TimeoutError(f"Kling motion control timed out after {timeout_seconds} seconds for task {task_id}")


def extract_kling_task_id(response: dict[str, Any]) -> str:
    for path in ("data.task_id", "data.taskId", "task_id", "taskId", "id"):
        value = _field(response, path)
        if value:
            return str(value)
    return ""


def extract_kling_status(response: dict[str, Any]) -> str:
    for path in ("data.task_status", "data.status", "task_status", "status"):
        value = _field(response, path)
        if value:
            return str(value).lower()
    return ""


def extract_kling_video_url(response: dict[str, Any]) -> str:
    candidate_paths = (
        "data.task_result.videos",
        "data.result.videos",
        "task_result.videos",
        "result.videos",
        "data.video.url",
        "video.url",
        "data.url",
        "url",
    )
    for path in candidate_paths:
        value = _field(response, path)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    url = item.get("url") or item.get("video_url") or item.get("videoUrl")
                    if isinstance(url, str) and is_public_url(url):
                        return url
                elif isinstance(item, str) and is_public_url(item):
                    return item
        if isinstance(value, str) and is_public_url(value):
            return value
    return ""


def _base64url_json(value: dict[str, Any]) -> str:
    return _base64url_bytes(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _base64url_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def select_posts_to_download(
    posts: list[TikTokVideo],
    *,
    last_24_hours: bool = False,
    now: datetime | None = None,
) -> list[TikTokVideo]:
    newest_first = sorted(posts, key=lambda post: post.posted_at, reverse=True)
    if last_24_hours:
        return filter_recent_posts(newest_first, now=now)

    return newest_first[:1]


def downloadLatestTikTokVideo(
    profile_input: str,
    *,
    last_24_hours: bool = False,
    videos_dir: Path = DEFAULT_VIDEOS_DIR,
    results_per_page: int = 10,
) -> list[Path]:
    """Download TikTok videos and write metadata.

    By default this downloads the latest visible video for the profile, regardless of age.
    Pass last_24_hours=True to download every visible video posted in the last 24 hours.
    """
    token = os.environ.get("APIFY_TOKEN") or load_dotenv_value(PROJECT_ROOT / ".env", "APIFY_TOKEN")
    if not token:
        raise RuntimeError("Set APIFY_TOKEN before running the TikTok downloader")

    handle = parse_profile_handle(profile_input)
    metadata_path = videos_dir / "metadata.csv"
    seen_video_ids = load_seen_video_ids(metadata_path)

    actor_items = fetch_profile_posts_from_apify(build_actor_input(handle, results_per_page), token)
    posts = [normalize_post(item, handle) for item in actor_items]
    selected_posts = select_posts_to_download(posts, last_24_hours=last_24_hours)
    downloaded_paths: list[Path] = []

    for post in selected_posts:
        if post.video_id in seen_video_ids:
            continue
        if not post.download_url:
            print(f"Skipping {post.video_id or post.source_url}: Apify did not return an MP4 URL")
            continue

        output_path = videos_dir / make_video_filename(post)
        if output_path.exists():
            seen_video_ids.add(post.video_id)
            continue

        download_mp4(post.download_url, output_path, token)
        append_metadata(metadata_path, post, output_path)
        downloaded_paths.append(output_path)
        seen_video_ids.add(post.video_id)

    return downloaded_paths


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


def _field(item: dict[str, Any], path: str) -> Any:
    if path in item:
        return item[path]

    value: Any = item
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _parse_apify_datetime(create_time_iso: Any, create_time: Any) -> datetime:
    if create_time_iso:
        value = str(create_time_iso).replace("Z", "+00:00")
        return datetime.fromisoformat(value).astimezone(timezone.utc)

    if create_time:
        return datetime.fromtimestamp(int(create_time), timezone.utc)

    raise ValueError("Apify item did not include createTimeISO or createTime")


def _extract_hashtags(raw_hashtags: Any) -> list[str]:
    if not isinstance(raw_hashtags, list):
        return []

    tags: list[str] = []
    for tag in raw_hashtags:
        if isinstance(tag, dict) and tag.get("name"):
            tags.append(str(tag["name"]))
        elif isinstance(tag, str):
            tags.append(tag)
    return tags


def _extract_download_url(item: dict[str, Any]) -> str:
    media_urls = _field(item, "mediaUrls")
    if isinstance(media_urls, list):
        for url in media_urls:
            if isinstance(url, str) and url.startswith("http"):
                return url

    for path in (
        "downloadUrl",
        "downloadURL",
        "videoUrl",
        "videoMeta.downloadAddr",
        "videoMeta.playAddr",
        "videoMeta.downloadUrl",
    ):
        value = _field(item, path)
        if isinstance(value, str) and value.startswith("http"):
            return value

    return ""


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Download TikTok videos through Apify.")
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE_INPUT,
        help=f"TikTok handle or profile URL. Defaults to {DEFAULT_PROFILE_INPUT}",
    )
    parser.add_argument(
        "--last-24-hours",
        action="store_true",
        help="Download all visible videos posted in the last 24 hours instead of only the latest video.",
    )
    parser.add_argument(
        "--results-per-page",
        type=int,
        default=10,
        help="Number of recent profile videos for Apify to inspect.",
    )
    args = parser.parse_args()

    downloaded = downloadLatestTikTokVideo(
        args.profile,
        last_24_hours=args.last_24_hours,
        results_per_page=args.results_per_page,
    )
    if not downloaded:
        mode = "the last 24 hours" if args.last_24_hours else "the latest video"
        print(f"No new TikTok videos were downloaded for {mode}.")
        return

    for path in downloaded:
        print(f"Downloaded {path}")


if __name__ == "__main__":
    main()
