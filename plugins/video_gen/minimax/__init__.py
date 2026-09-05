"""MiniMax video generation backend (Hailuo 2.3 v1 + H3 v2).

User-facing surface: pick **MiniMax-H3** (default) or **MiniMax-Hailuo-2.3**
for all modes. The plugin auto-routes to the right MiniMax API version
based on the resolved model:

  model MiniMax-H3              → v2 API (content array, 2K, 4-15s)
  model MiniMax-Hailuo-2.3      → v1 API (legacy, 768P/1080P, 6/10s)

Mode routing inside each API version is driven by which inputs are provided:

  prompt only                          → text-to-video
  prompt + 1 image                     → image-to-video (first frame)
  prompt + 2 images                    → image-to-video (first + last frame)
  prompt + 3+ images                   → reference-to-video (role=reference_image)
  prompt + reference video(s)          → reference-to-video (role=reference_video)
  prompt + reference audio(s)          → reference-to-video (role=reference_audio,
                                        must be paired with an image or video)

The agent never sees the routing — it just calls
``video_generate(prompt=..., image_url=..., reference_image_urls=...,
reference_video_urls=..., reference_audio_urls=...)``.
``image_url`` merges with ``reference_image_urls`` as the first input;
any reference video/audio forces the whole request into reference mode.

Authentication: ``MINIMAX_API_KEY`` env var. Set in ``~/.hermes/.env`` or
exported directly. The API host defaults to ``https://api.minimax.io``;
override with ``MINIMAX_API_HOST`` for regional endpoints.

Output: MP4 saved to ``$HERMES_HOME/cache/videos/`` so it persists beyond
MiniMax's time-limited URL expiry. The absolute path is returned as ``video``
so Hermes' Deliverable Mode ships it natively to Discord / Telegram.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid as uuid_module
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from agent.video_gen_provider import (
    VideoGenProvider,
    error_response,
    save_bytes_video,
    success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io"
DEFAULT_MODEL = "MiniMax-H3"
LEGACY_MODEL = "MiniMax-Hailuo-2.3"
H3_MODEL = DEFAULT_MODEL  # canonical H3 (v2 API) wire name
DEFAULT_DURATION = 6  # seconds; H3 supports 4-15, Hailuo 2.3 supports 6 or 10
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_RESOLUTION = "768P"  # v1 enum: 768P | 1080P (H3 is always 2K)
DEFAULT_TIMEOUT_SECONDS = 300  # 5 minutes for polling
DEFAULT_POLL_INTERVAL_SECONDS = 5

VALID_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4"}
VALID_RESOLUTIONS = {"768P", "1080P"}
VALID_DURATIONS = {6, 10}

# MiniMax H3 (v2 API) constants
H3_RESOLUTION = "2K"
H3_MIN_DURATION = 4
H3_MAX_DURATION = 15
H3_VALID_RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
# Unified-surface ratios the H3 v2 API does not offer, mapped to nearest.
H3_RATIO_MAP = {"3:2": "16:9", "2:3": "9:16"}
H3_MAX_REFERENCE_IMAGES = 9
H3_MAX_REFERENCE_VIDEOS = 3
H3_MAX_REFERENCE_AUDIO = 3
H3_MAX_TEXT_CHARS = 7000
H3_MAX_BODY_BYTES = 60 * 1024 * 1024  # v2 hard limit is 64 MB; headroom for JSON overhead


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

_MODELS: Dict[str, Dict[str, Any]] = {
    "MiniMax-H3": {
        "display": "MiniMax H3",
        "speed": "~60-300s",
        "strengths": (
            "New-generation multimodal video model (v2 API). Text-to-video, "
            "image-to-video (first/last frame), reference-to-video. "
            "2K output, 4-15s."
        ),
        "price": "see https://platform.minimax.io/pricing",
        "modalities": ["text", "image"],
        "api_version": "v2",
    },
    "MiniMax-Hailuo-2.3": {
        "display": "MiniMax Hailuo 2.3",
        "speed": "~60-300s",
        "strengths": (
            "Legacy v1 API model. Text-to-video, image-to-video, "
            "first-and-last-frame, character-reference (face-consistency). "
            "768P/1080P, 6 or 10s."
        ),
        "price": "see https://platform.minimax.io/pricing",
        "modalities": ["text", "image"],
        "api_version": "v1",
    },
}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _resolve_minimax_credentials() -> Tuple[str, str]:
    """Return ``(api_key, base_url)`` from environment.

    ``MINIMAX_API_KEY`` is required. ``MINIMAX_API_HOST`` is optional and
    defaults to ``https://api.minimax.io``.
    """
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    base_url = os.getenv("MINIMAX_API_HOST", "").strip()
    if not base_url:
        base_url = DEFAULT_MINIMAX_BASE_URL
    base_url = base_url.rstrip("/")
    return api_key, base_url


def _minimax_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def _build_image_input(value: str) -> Optional[Dict[str, str]]:
    """Return a MiniMax image input dict from a URL or data-URI.

    MiniMax accepts ``data:image/<ext>;base64,...`` and ``https://`` URLs.
    Returns a dict with a ``url`` key (or ``base64_data`` for base64), or
    ``None`` if the input cannot be normalised.
    """
    ref = (value or "").strip()
    if not ref:
        return None
    lower = ref.lower()
    if lower.startswith(("http://", "https://")):
        return {"url": ref}
    if lower.startswith("data:image/"):
        return {"url": ref}
    # Local file — read and base64 encode
    path = Path(ref).expanduser()
    if not path.is_file():
        return None
    import mimetypes

    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"url": f"data:{mime};base64,{encoded}"}


def _build_media_input(value: str) -> Optional[Dict[str, str]]:
    """Return a MiniMax media input dict from a URL, data-URI or local file.

    Used for reference video/audio inputs. Accepts ``https://`` URLs,
    ``data:video/...`` / ``data:audio/...`` data URIs, and local files
    (base64-encoded with a lowercase mime prefix, per the v2 schema).
    """
    ref = (value or "").strip()
    if not ref:
        return None
    lower = ref.lower()
    if lower.startswith(("http://", "https://")):
        return {"url": ref}
    if lower.startswith(("data:video/", "data:audio/")):
        return {"url": ref}
    path = Path(ref).expanduser()
    if not path.is_file():
        return None
    import mimetypes

    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"url": f"data:{mime};base64,{encoded}"}


def _build_subject_reference(
    subject_reference: Optional[str],
) -> Optional[List[Dict[str, Any]]]:
    """Build MiniMax ``subject_reference`` list from a URL/data-URI."""
    if not subject_reference:
        return None
    ref = _build_image_input(subject_reference)
    if not ref:
        return None
    return [{"type": "character", "image_url": ref}]


# ---------------------------------------------------------------------------
# Async HTTP helpers
# ---------------------------------------------------------------------------


async def _submit_minimax_video_request(
    client: httpx.AsyncClient,
    payload: Dict[str, Any],
    *,
    api_key: str,
    base_url: str,
) -> str:
    """POST the MiniMax video generation payload; return ``task_id``."""
    response = await client.post(
        f"{base_url}/v1/video_generation",
        headers=_minimax_headers(api_key),
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    # MiniMax returns {"task_id": "..."} or {"request_id": "..."}
    task_id = body.get("task_id") or body.get("request_id") or ""
    if not task_id:
        raise RuntimeError(
            f"MiniMax video response did not contain task_id: {body}"
        )
    return str(task_id)


async def _poll_minimax_task(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    api_key: str,
    base_url: str,
    timeout_seconds: int,
    poll_interval: int,
) -> Dict[str, Any]:
    """Poll until the MiniMax task reaches ``Success`` or a terminal state."""
    elapsed = 0.0
    last_status = ""
    while elapsed < timeout_seconds:
        response = await client.get(
            f"{base_url}/v1/query/video_generation",
            headers=_minimax_headers(api_key),
            params={"task_id": task_id},
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        last_status = str(body.get("status", "")).strip()

        if last_status == "Success":
            return {"status": "Success", "body": body}
        if last_status in ("Fail", "Failed", "Expired", "Cancelled", "Error"):
            return {"status": last_status, "body": body}

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return {"status": "timeout", "body": {"status": last_status}}


async def _fetch_video_bytes(
    client: httpx.AsyncClient,
    download_url: str,
    timeout: int = 120,
) -> bytes:
    """Download video bytes from the MiniMax CDN / file URL."""
    response = await client.get(download_url, timeout=timeout)
    response.raise_for_status()
    return response.content


# ---------------------------------------------------------------------------
# MiniMax H3 (v2 API) — payload building, submit, poll
# ---------------------------------------------------------------------------


def _is_h3_model(model: Optional[str]) -> bool:
    """True when the model identifier targets the H3 (v2 API) backend.

    Exact match on the canonical id, plus a bare ``h3`` alias. Substring
    matching is deliberately avoided — a future ``h30``/``h3b`` model
    must NOT route to the v2 path.
    """
    if not model:
        return False
    normalized = str(model).strip().lower()
    return normalized == H3_MODEL.lower() or normalized == "h3"


# Markers in H3 error responses that indicate the current token plan does
# NOT include H3 access — distinct from caller errors like missing_prompt or
# payload_too_large. When the provider's generate() sees these, it falls
# back to the legacy Hailuo 2.3 (v1 API) model. The user explicitly requested
# this automatic fallback (Jul 2026) because H3 access depends on plan tier
# and the v1 model is broadly available.
_H3_PLAN_TIER_UNAVAILABLE_MARKERS = (
    "does not currently support",
    "TokenPlan",
    "Insufficient balance",
    "Insufficient credit",
    "plan tier",
    "(2013)",
)


def _is_h3_plan_tier_unavailable(response: Dict[str, Any]) -> bool:
    """True when an H3 response indicates plan/credit unavailability.

    Conservative: matches only ``api_error`` responses whose error string
    contains a plan-tier marker. Caller errors (missing_prompt,
    payload_too_large, auth_required, invalid_params) are surfaced
    unchanged so the user still sees them.
    """
    if not isinstance(response, dict):
        return False
    if response.get("error_type") != "api_error":
        return False
    msg = str(response.get("error") or "")
    return any(marker in msg for marker in _H3_PLAN_TIER_UNAVAILABLE_MARKERS)


def _build_h3_image_item(url: str, role: str) -> Dict[str, Any]:
    """Build one ``image_url`` content item for the H3 v2 API.

    The v2 schema requires ``image_url`` to be an object with a ``url``
    key (public URL, ``mm_file://{file_id}``, or lowercase data URI).
    """
    return {"type": "image_url", "image_url": {"url": url}, "role": role}


def _build_h3_content(
    prompt_text: str,
    image_url: Optional[str],
    reference_image_urls: Optional[List[str]],
    reference_video_urls: Optional[List[str]] = None,
    reference_audio_urls: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Build the v2 ``content`` array; return ``(content, mode)``.

    Mode is ``"text"``, ``"image"`` (first/last frame) or ``"reference"``.

    Reference mode is entered when **video or audio references are
    present** (image-to-video and reference modes are mutually exclusive
    on the API, so any ``reference_*`` role forces ALL images into
    ``reference_image`` roles), or when 3+ images are provided.
    ``image_url`` merges with ``reference_image_urls`` as the first input.

    Raises ``ValueError`` if audio references are given with no image or
    video reference (audio alone is not a valid reference mode on the API).
    """
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]

    image_candidates: List[str] = []
    if image_url and image_url.strip():
        image_candidates.append(image_url.strip())
    for ref in reference_image_urls or []:
        if ref and ref.strip():
            image_candidates.append(ref.strip())

    image_urls: List[str] = []  # normalized image URLs (only valid inputs)
    for cand in image_candidates:
        img = _build_image_input(cand)
        if img and img.get("url"):
            image_urls.append(img["url"])

    video_urls: List[str] = []
    for ref in reference_video_urls or []:
        if ref and ref.strip():
            media = _build_media_input(ref)
            if media and media.get("url"):
                video_urls.append(media["url"])

    audio_urls: List[str] = []
    for ref in reference_audio_urls or []:
        if ref and ref.strip():
            media = _build_media_input(ref)
            if media and media.get("url"):
                audio_urls.append(media["url"])

    if not image_urls and not video_urls and not audio_urls:
        return content, "text"

    if audio_urls and not image_urls and not video_urls:
        raise ValueError(
            "reference audio requires at least one image or video reference "
            "(audio alone is not a valid MiniMax H3 reference mode)"
        )

    if video_urls or audio_urls:
        # Any reference video/audio → reference mode; all images become
        # reference_image roles (i2v and reference modes are exclusive).
        for url in image_urls[:H3_MAX_REFERENCE_IMAGES]:
            content.append(_build_h3_image_item(url, "reference_image"))
        for url in video_urls[:H3_MAX_REFERENCE_VIDEOS]:
            content.append(
                {"type": "video_url", "video_url": {"url": url}, "role": "reference_video"}
            )
        for url in audio_urls[:H3_MAX_REFERENCE_AUDIO]:
            content.append(
                {"type": "audio_url", "audio_url": {"url": url}, "role": "reference_audio"}
            )
        return content, "reference"

    if len(image_urls) == 1:
        content.append(_build_h3_image_item(image_urls[0], "first_frame"))
        return content, "image"

    if len(image_urls) == 2:
        content.append(_build_h3_image_item(image_urls[0], "first_frame"))
        content.append(_build_h3_image_item(image_urls[1], "last_frame"))
        return content, "image"

    # 3+ images → reference-to-video; all images become references.
    for url in image_urls[:H3_MAX_REFERENCE_IMAGES]:
        content.append(_build_h3_image_item(url, "reference_image"))
    return content, "reference"


def _resolve_h3_ratio(
    mode: str,
    aspect_ratio: Optional[str],
) -> Tuple[str, Optional[str]]:
    """Return ``(ratio, adjustment_note)`` for the H3 v2 API.

    Image (i2v) mode: ratio is always ``adaptive`` — the API derives it
    from the input image; concrete values are ignored.
    Reference (r2v) mode: ratio is optional and defaults to ``adaptive``;
    a concrete v2 ratio may be passed through when explicitly requested.
    Text mode: ratio is required and cannot be ``adaptive`` — map the
    unified-surface value onto the v2 enum.
    """
    if mode == "image":
        return "adaptive", None
    if mode == "reference":
        ar = (aspect_ratio or "").strip()
        if ar in H3_VALID_RATIOS:
            return ar, None
        return "adaptive", None
    ar = (aspect_ratio or "").strip() or DEFAULT_ASPECT_RATIO
    if ar == "adaptive":
        return DEFAULT_ASPECT_RATIO, "adaptive->16:9"
    mapped = H3_RATIO_MAP.get(ar)
    if mapped:
        return mapped, f"{ar}->{mapped}"
    if ar in H3_VALID_RATIOS:
        return ar, None
    return DEFAULT_ASPECT_RATIO, f"{ar}->16:9"


def _build_h3_payload(
    *,
    prompt_text: str,
    image_url: Optional[str],
    reference_image_urls: Optional[List[str]],
    reference_video_urls: Optional[List[str]] = None,
    reference_audio_urls: Optional[List[str]] = None,
    duration: Optional[int],
    aspect_ratio: Optional[str],
) -> Tuple[Dict[str, Any], Optional[str], str]:
    """Build the H3 v2 request body (pure; no I/O).

    Returns ``(payload, ratio_note, mode)``. Raises ``ValueError`` for
    invalid prompts (empty, over the 7000-char limit) or invalid
    reference combinations (audio without image/video reference).
    """
    if not prompt_text or not prompt_text.strip():
        raise ValueError("prompt is required for MiniMax H3 video generation.")

    if len(prompt_text) > H3_MAX_TEXT_CHARS:
        raise ValueError(
            f"MiniMax H3 text prompt exceeds the {H3_MAX_TEXT_CHARS}-character "
            f"limit ({len(prompt_text)} chars). Shorten the prompt."
        )

    content, mode = _build_h3_content(
        prompt_text,
        image_url,
        reference_image_urls,
        reference_video_urls,
        reference_audio_urls,
    )

    dur = int(duration) if duration is not None else DEFAULT_DURATION
    dur = max(H3_MIN_DURATION, min(H3_MAX_DURATION, dur))

    ratio, ratio_note = _resolve_h3_ratio(mode, aspect_ratio)

    payload: Dict[str, Any] = {
        "model": H3_MODEL,
        "content": content,
        "resolution": H3_RESOLUTION,
        "duration": dur,
        "ratio": ratio,
    }
    return payload, ratio_note, mode


def _minimax_error_message(raw: Any, fallback: str) -> str:
    """Extract a human message from MiniMax's error envelope.

    Error bodies look like ``{"type":"error","error":{"type":...,"message":...,
    "http_code":...},"request_id":...}``.
    """
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict):
            err = raw.get("error") or {}
            if isinstance(err, dict):
                message = err.get("message")
                if message:
                    return str(message)
            message = raw.get("message")
            if message:
                return str(message)
    except Exception:
        pass
    return fallback


async def _submit_h3_task(
    client: httpx.AsyncClient,
    payload: Dict[str, Any],
    *,
    api_key: str,
    base_url: str,
) -> str:
    """POST the H3 v2 payload; return ``task_id``."""
    response = await client.post(
        f"{base_url}/v2/video_generation",
        headers=_minimax_headers(api_key),
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    task_id = body.get("task_id") or ""
    if not task_id:
        raise RuntimeError(
            f"MiniMax H3 response did not contain task_id: {body}"
        )
    return str(task_id)


async def _poll_h3_task(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    api_key: str,
    base_url: str,
    timeout_seconds: int,
    poll_interval: int,
) -> Dict[str, Any]:
    """Poll until the H3 v2 task reaches a terminal state.

    v2 query endpoint: ``GET {base}/v2/query/video_generation/{task_id}``
    (task_id in the PATH, unlike v1's query param). Statuses:
    queued / running / succeeded / failed / cancelled / expired.
    """
    elapsed = 0.0
    last_status = ""
    while elapsed < timeout_seconds:
        response = await client.get(
            f"{base_url}/v2/query/video_generation/{task_id}",
            headers=_minimax_headers(api_key),
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        task = body.get("task") if isinstance(body, dict) else None
        if not isinstance(task, dict):
            task = body
        last_status = str(task.get("status", "")).strip()

        if last_status == "succeeded":
            return {"status": "succeeded", "body": body}
        if last_status in ("failed", "cancelled", "expired"):
            return {"status": last_status, "body": body}

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return {"status": "timeout", "body": {"task": {"status": last_status}}}


def _extract_h3_result(body: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str]]:
    """Return ``(status, video_url, error_message)`` from a v2 query body."""
    task = body.get("task") if isinstance(body, dict) else None
    if not isinstance(task, dict):
        task = body
    status = str(task.get("status", "")).strip() or "unknown"

    content = task.get("content") or {}
    if isinstance(content, list):
        content = content[0] if content else {}
    url: Optional[str] = None
    if isinstance(content, dict) and content.get("url"):
        url = str(content["url"])

    err = task.get("error") or {}
    message: Optional[str] = None
    if isinstance(err, dict):
        message = err.get("message") or err.get("code")
    return status, url, (str(message) if message else None)


# ---------------------------------------------------------------------------
# Sync bridge (matches xAI pattern)
# ---------------------------------------------------------------------------


def _run_coroutine(coro, operation_label: str, **err_kwargs) -> Dict[str, Any]:
    """Run an async coroutine from a sync context; return error_response on failure."""
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    except Exception as exc:
        logger.warning(
            "MiniMax video %s unexpected failure: %s",
            operation_label,
            exc,
            exc_info=True,
        )
        return error_response(
            error=f"MiniMax video {operation_label} failed: {exc}",
            error_type="api_error",
            provider="minimax",
            **err_kwargs,
        )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class MinimaxVideoGenProvider(VideoGenProvider):
    """MiniMax Hailuo video generation backend."""

    @property
    def name(self) -> str:
        return "minimax"

    @property
    def display_name(self) -> str:
        return "MiniMax"

    def is_available(self) -> bool:
        api_key, _ = _resolve_minimax_credentials()
        return bool(api_key)

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": mid, **meta} for mid, meta in _MODELS.items()]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "MiniMax",
            "badge": "paid",
            "tag": (
                "MiniMax-H3 (v2): text-to-video, image-to-video (first/last "
                "frame), reference-to-video, 2K, 4-15s. "
                "MiniMax-Hailuo-2.3 (v1): legacy text/image/first-last/face-consistency."
            ),
            "env_vars": [
                {
                    "key": "MINIMAX_API_KEY",
                    "prompt": "MiniMax API key",
                    "url": "https://platform.minimax.io/apikey",
                },
            ],
        }

    def capabilities(self, model: Optional[str] = None) -> Dict[str, Any]:
        """Per-model capabilities; defaults to the active default (H3)."""
        target = (model or "").strip() or DEFAULT_MODEL
        if not _is_h3_model(target):
            return {
                "modalities": ["text", "image"],
                "aspect_ratios": sorted(VALID_ASPECT_RATIOS),
                "resolutions": sorted(VALID_RESOLUTIONS),
                "max_duration": 10,
                "min_duration": 6,
                "supports_audio": False,
                "supports_negative_prompt": False,
                "max_reference_images": 0,
                "max_reference_videos": 0,
                "max_reference_audio": 0,
            }
        return {
            "modalities": ["text", "image"],
            "aspect_ratios": sorted(H3_VALID_RATIOS),
            "resolutions": [H3_RESOLUTION],
            "max_duration": H3_MAX_DURATION,
            "min_duration": H3_MIN_DURATION,
            "supports_audio": False,
            "supports_negative_prompt": False,
            "max_reference_images": H3_MAX_REFERENCE_IMAGES,
            "max_reference_videos": H3_MAX_REFERENCE_VIDEOS,
            "max_reference_audio": H3_MAX_REFERENCE_AUDIO,
        }

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        reference_video_urls: Optional[List[str]] = None,
        reference_audio_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        resolved_model = (model or "").strip() or DEFAULT_MODEL
        if _is_h3_model(resolved_model):
            # MiniMax H3 → v2 API (content array, 2K, 4-15s)
            h3_result = _run_coroutine(
                _generate_h3_video_async(
                    prompt=prompt,
                    image_url=image_url,
                    reference_image_urls=reference_image_urls,
                    reference_video_urls=reference_video_urls,
                    reference_audio_urls=reference_audio_urls,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    seed=seed,
                ),
                operation_label="generation (H3 v2)",
                model=resolved_model,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )
            # Automatic fallback: when H3 is unavailable on the current
            # token plan (e.g. error 2013 "TokenPlan or Credit does not
            # currently support MiniMax-H3 series models"), fall through to
            # the legacy Hailuo 2.3 (v1 API) model. Triggered only for
            # plan-tier errors — caller errors (missing_prompt,
            # payload_too_large, auth_required, invalid_params) are
            # surfaced unchanged.
            #
            # Note: Hailuo 2.3 only supports 6s/10s durations and 768P/1080P;
            # requested duration < 6 or > 10 will be clamped to 6s, and the
            # 720p resolution will be replaced. Reference video/audio
            # inputs are rejected by the v1 API (it will return its own
            # clear error rather than silently dropping them).
            if _is_h3_plan_tier_unavailable(h3_result):
                logger.warning(
                    "MiniMax H3 unavailable on current plan (error_type=%s); "
                    "falling back to %s. Requested duration=%s will be "
                    "clamped to 6s by the v1 API if outside {{6,10}}.",
                    h3_result.get("error_type"),
                    LEGACY_MODEL,
                    duration,
                )
                return _run_coroutine(
                    _generate_minimax_video_async(
                        prompt=prompt,
                        model=LEGACY_MODEL,
                        image_url=image_url,
                        reference_image_urls=reference_image_urls,
                        reference_video_urls=reference_video_urls,
                        reference_audio_urls=reference_audio_urls,
                        duration=duration,
                        aspect_ratio=aspect_ratio,
                        resolution=resolution,
                        seed=seed,
                    ),
                    operation_label="generation (Hailuo 2.3 fallback)",
                    model=LEGACY_MODEL,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                )
            return h3_result
        # MiniMax Hailuo 2.3 / other v1 models → legacy v1 API
        return _run_coroutine(
            _generate_minimax_video_async(
                prompt=prompt,
                model=resolved_model,
                image_url=image_url,
                reference_image_urls=reference_image_urls,
                reference_video_urls=reference_video_urls,
                reference_audio_urls=reference_audio_urls,
                duration=duration,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                seed=seed,
            ),
            operation_label="generation",
            model=resolved_model,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
        )


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _generate_minimax_video_async(
    *,
    prompt: str,
    model: Optional[str],
    image_url: Optional[str],
    reference_image_urls: Optional[List[str]],
    reference_video_urls: Optional[List[str]] = None,
    reference_audio_urls: Optional[List[str]] = None,
    duration: Optional[int],
    aspect_ratio: str,
    resolution: str,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    api_key, base_url = _resolve_minimax_credentials()
    if not api_key:
        return error_response(
            error=(
                "No MiniMax API key found. Set MINIMAX_API_KEY in "
                "~/.hermes/.env or export it directly. "
                "Get your key at https://platform.minimax.io/apikey"
            ),
            error_type="auth_required",
            provider="minimax",
            model=model or LEGACY_MODEL,
            prompt=prompt or "",
        )

    prompt_text = (prompt or "").strip()
    if not prompt_text:
        return error_response(
            error="prompt is required for MiniMax video generation.",
            error_type="missing_prompt",
            provider="minimax",
            model=model or LEGACY_MODEL,
            prompt="",
        )

    # v1 (Hailuo) has no reference video/audio roles — fail loudly rather
    # than silently dropping the caller's references.
    if reference_video_urls or reference_audio_urls:
        return error_response(
            error=(
                "reference video/audio are not supported by "
                "MiniMax-Hailuo-2.3 (v1 API). Use the MiniMax-H3 model "
                "for reference-to-video with video/audio inputs."
            ),
            error_type="invalid_params",
            provider="minimax",
            model=model or LEGACY_MODEL,
            prompt=prompt_text,
        )

    # Clamp duration to MiniMax's valid set
    dur = int(duration) if duration is not None else DEFAULT_DURATION
    if dur not in VALID_DURATIONS:
        dur = DEFAULT_DURATION

    # Clamp resolution
    res = (resolution or "").strip().upper()
    if res not in VALID_RESOLUTIONS:
        res = DEFAULT_RESOLUTION

    # Clamp aspect ratio
    ar = (aspect_ratio or "").strip()
    if ar not in VALID_ASPECT_RATIOS:
        ar = DEFAULT_ASPECT_RATIO

    resolved_model = (model or "").strip() or LEGACY_MODEL

    # Determine modality and build request payload
    first_frame = None
    last_frame = None
    subject_ref = None

    # Check for first-and-last-frame inputs (passed via reference_image_urls
    # when there are exactly 1 or 2 items: [first_frame] or [first_frame, last_frame])
    # The Hermes schema uses reference_image_urls for general reference images;
    # first_frame / last_frame are passed as the first two items if present.
    ref_urls = reference_image_urls or []
    img_url_input = (image_url or "").strip() or None

    if ref_urls:
        first_input = ref_urls[0]
        first_frame = _build_image_input(first_input)
        if len(ref_urls) >= 2:
            last_frame = _build_image_input(ref_urls[1])

    modality_used = "image" if (img_url_input or first_frame) else "text"

    # Build MiniMax request payload
    payload: Dict[str, Any] = {
        "model": resolved_model,
        "prompt": prompt_text,
        "duration": dur,
        "resolution": res,
    }

    if img_url_input:
        img_input = _build_image_input(img_url_input)
        if img_input:
            payload["image_url"] = img_input["url"]

    if first_frame:
        payload["first_frame_image"] = first_frame["url"]
        if last_frame:
            payload["last_frame_image"] = last_frame["url"]

    # subject_reference: character face-consistency (passed as first reference_image_urls
    # item when mode is "subject_reference" — detected by the presence of a 3rd item
    # or a specific kwarg; for simplicity we support the first frame as a subject
    # reference when there are 3+ reference images)
    if len(ref_urls) >= 3:
        subject_ref = _build_subject_reference(ref_urls[0])
        if subject_ref:
            payload["subject_reference"] = subject_ref

    # Optional seed
    if seed is not None:
        payload["seed"] = int(seed)

    async with httpx.AsyncClient() as client:
        try:
            task_id = await _submit_minimax_video_request(
                client, payload, api_key=api_key, base_url=base_url
            )
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = exc.response.text[:500]
            except Exception:
                pass
            return error_response(
                error=(
                    f"MiniMax submit failed ({exc.response.status_code}): "
                    f"{detail or exc}"
                ),
                error_type="api_error",
                provider="minimax",
                model=resolved_model,
                prompt=prompt_text,
                aspect_ratio=ar,
            )

        poll_result = await _poll_minimax_task(
            client,
            task_id,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
        )

    status = poll_result["status"]
    body = poll_result["body"]

    if status == "Success":
        video_data = body.get("video") or {}
        download_url = video_data.get("url") or ""

        # Try file_id → retrieve endpoint if no direct URL
        if not download_url:
            file_id = video_data.get("file_id") or body.get("file_id") or ""
            if file_id:
                try:
                    async with httpx.AsyncClient() as client2:
                        file_resp = await client2.get(
                            f"{base_url}/v1/files/retrieve",
                            headers=_minimax_headers(api_key),
                            params={"file_id": file_id},
                            timeout=30,
                        )
                        file_resp.raise_for_status()
                        file_body = file_resp.json()
                        download_url = (
                            file_body.get("download_url")
                            or file_body.get("url")
                            or ""
                        )
                except Exception as exc2:
                    logger.warning(
                        "MiniMax file retrieve failed (file_id=%s): %s",
                        file_id,
                        exc2,
                    )

        video_bytes: Optional[bytes] = None
        local_path_str = ""

        if download_url:
            try:
                async with httpx.AsyncClient() as client3:
                    video_bytes = await _fetch_video_bytes(client3, download_url)
            except Exception as exc3:
                logger.warning(
                    "MiniMax video download failed (url=%s): %s",
                    download_url,
                    exc3,
                )

        if video_bytes:
            saved = save_bytes_video(
                video_bytes,
                prefix="minimax",
                extension="mp4",
            )
            local_path_str = str(saved.resolve())
        elif download_url:
            # Could not download; return the CDN URL — gateway may fetch it directly
            local_path_str = download_url
        else:
            return error_response(
                error="MiniMax returned no video URL or file_id in response.",
                error_type="empty_response",
                provider="minimax",
                model=resolved_model,
                prompt=prompt_text,
                aspect_ratio=ar,
            )

        extra: Dict[str, Any] = {
            "task_id": task_id,
            "modality": modality_used,
        }
        if download_url:
            extra["cdn_url"] = download_url
        if duration is not None:
            extra["duration_requested"] = int(duration)

        return success_response(
            video=local_path_str,
            model=resolved_model,
            prompt=prompt_text,
            modality=modality_used,
            aspect_ratio=ar,
            duration=dur,
            provider="minimax",
            extra=extra,
        )

    if status == "timeout":
        return error_response(
            error=(
                f"Timed out waiting for MiniMax video after "
                f"{DEFAULT_TIMEOUT_SECONDS}s. "
                "MiniMax video generation can take 1-5 minutes. "
                "Try a shorter duration (6s) or try again."
            ),
            error_type="timeout",
            provider="minimax",
            model=resolved_model,
            prompt=prompt_text,
            aspect_ratio=ar,
        )

    message = (
        (body.get("error", {}) or {}).get("message")
        or body.get("message")
        or body.get("status")
        or f"MiniMax task ended with status '{status}'"
    )
    return error_response(
        error=str(message),
        error_type=f"minimax_{status.lower()}",
        provider="minimax",
        model=resolved_model,
        prompt=prompt_text,
        aspect_ratio=ar,
    )


# ---------------------------------------------------------------------------
# Async core — MiniMax H3 (v2 API)
# ---------------------------------------------------------------------------


async def _generate_h3_video_async(
    *,
    prompt: str,
    image_url: Optional[str],
    reference_image_urls: Optional[List[str]],
    reference_video_urls: Optional[List[str]] = None,
    reference_audio_urls: Optional[List[str]] = None,
    duration: Optional[int],
    aspect_ratio: str,
    resolution: str,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    api_key, base_url = _resolve_minimax_credentials()
    if not api_key:
        return error_response(
            error=(
                "No MiniMax API key found. Set MINIMAX_API_KEY in "
                "~/.hermes/.env or export it directly. "
                "Get your key at https://platform.minimax.io/apikey"
            ),
            error_type="auth_required",
            provider="minimax",
            model=H3_MODEL,
            prompt=prompt or "",
        )

    prompt_text = (prompt or "").strip()
    if not prompt_text:
        return error_response(
            error="prompt is required for MiniMax H3 video generation.",
            error_type="missing_prompt",
            provider="minimax",
            model=H3_MODEL,
            prompt="",
        )

    try:
        payload, ratio_note, mode = _build_h3_payload(
            prompt_text=prompt_text,
            image_url=image_url,
            reference_image_urls=reference_image_urls,
            reference_video_urls=reference_video_urls,
            reference_audio_urls=reference_audio_urls,
            duration=duration,
            aspect_ratio=aspect_ratio,
        )
    except ValueError as exc:
        return error_response(
            error=str(exc),
            error_type="invalid_params",
            provider="minimax",
            model=H3_MODEL,
            prompt=prompt_text,
        )

    # 64 MB request-body guard (base64 data URIs inflate by ~33%).
    try:
        body_bytes = json.dumps(payload).encode("utf-8")
    except Exception:
        body_bytes = b""
    if len(body_bytes) > H3_MAX_BODY_BYTES:
        return error_response(
            error=(
                "MiniMax H3 request body exceeds the 64 MB API limit. "
                "Provide public HTTPS image URLs instead of base64 data "
                "URIs or local files."
            ),
            error_type="payload_too_large",
            provider="minimax",
            model=H3_MODEL,
            prompt=prompt_text,
        )

    async with httpx.AsyncClient() as client:
        try:
            task_id = await _submit_h3_task(
                client, payload, api_key=api_key, base_url=base_url
            )
        except httpx.HTTPStatusError as exc:
            detail = _minimax_error_message(exc.response.text, str(exc))
            return error_response(
                error=(
                    f"MiniMax H3 submit failed ({exc.response.status_code}): "
                    f"{detail}"
                ),
                error_type="api_error",
                provider="minimax",
                model=H3_MODEL,
                prompt=prompt_text,
                aspect_ratio=payload.get("ratio") or "",
            )
        except Exception as exc:
            logger.warning(
                "MiniMax H3 submit unexpected failure: %s", exc, exc_info=True
            )
            return error_response(
                error=f"MiniMax H3 submit failed: {exc}",
                error_type="api_error",
                provider="minimax",
                model=H3_MODEL,
                prompt=prompt_text,
            )

        poll_result = await _poll_h3_task(
            client,
            task_id,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
        )

    status = poll_result["status"]
    body = poll_result["body"]
    task_status, download_url, task_error = _extract_h3_result(body)

    if status == "succeeded" and download_url:
        video_bytes: Optional[bytes] = None
        local_path_str = ""

        try:
            async with httpx.AsyncClient() as client2:
                video_bytes = await _fetch_video_bytes(client2, download_url)
        except Exception as exc3:
            logger.warning(
                "MiniMax H3 video download failed (url=%s): %s",
                download_url,
                exc3,
            )

        if video_bytes:
            saved = save_bytes_video(
                video_bytes,
                prefix="minimax",
                extension="mp4",
            )
            local_path_str = str(saved.resolve())
        elif download_url:
            # Could not download; return the CDN URL — gateway may fetch it directly
            local_path_str = download_url

        if not local_path_str:
            return error_response(
                error="MiniMax H3 returned no video URL in response.",
                error_type="empty_response",
                provider="minimax",
                model=H3_MODEL,
                prompt=prompt_text,
                aspect_ratio=payload.get("ratio") or "",
            )

        task_obj = body.get("task") if isinstance(body, dict) else {}
        if not isinstance(task_obj, dict):
            task_obj = {}

        extra: Dict[str, Any] = {
            "task_id": task_id,
            "api_version": "v2",
            "modality": "image" if mode != "text" else "text",
            "ratio": payload.get("ratio") or "",
        }
        if ratio_note:
            extra["ratio_adjusted"] = ratio_note
        if download_url:
            extra["cdn_url"] = download_url
        if duration is not None:
            extra["duration_requested"] = int(duration)
        if seed is not None:
            # v2 API has no seed parameter — make the drop explicit so the
            # caller knows reproducibility was not honored.
            extra["seed_dropped"] = True
        if resolution and resolution.strip().upper() != H3_RESOLUTION:
            # H3 is 2K-only; surface that the requested resolution was replaced.
            extra["resolution_requested"] = resolution
        if task_obj.get("task_type"):
            extra["task_type"] = str(task_obj["task_type"])
        if task_obj.get("ratio"):
            extra["actual_ratio"] = str(task_obj["ratio"])

        return success_response(
            video=local_path_str,
            model=H3_MODEL,
            prompt=prompt_text,
            modality="image" if mode != "text" else "text",
            aspect_ratio=payload.get("ratio") or "",
            duration=payload.get("duration") or 0,
            provider="minimax",
            extra=extra,
        )

    if status == "timeout":
        return error_response(
            error=(
                f"Timed out waiting for MiniMax H3 video after "
                f"{DEFAULT_TIMEOUT_SECONDS}s. "
                "H3 generation can take 1-5 minutes. "
                "Try a shorter duration or try again."
            ),
            error_type="timeout",
            provider="minimax",
            model=H3_MODEL,
            prompt=prompt_text,
        )

    message = task_error or (
        f"MiniMax H3 task ended with status '{task_status}'"
    )
    return error_response(
        error=str(message),
        error_type=f"minimax_{task_status.lower()}",
        provider="minimax",
        model=H3_MODEL,
        prompt=prompt_text,
    )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire ``MinimaxVideoGenProvider`` into the registry."""
    ctx.register_video_gen_provider(MinimaxVideoGenProvider())
