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

The agent never sees the routing — it just calls
``video_generate(prompt=..., image_url=..., reference_image_urls=...)``.
``image_url`` merges with ``reference_image_urls`` as the first input.

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
    """True when the model identifier targets the H3 (v2 API) backend."""
    return bool(model) and "h3" in str(model).lower()


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
) -> Tuple[List[Dict[str, Any]], str]:
    """Build the v2 ``content`` array; return ``(content, mode)``.

    Mode is ``"text"``, ``"image"`` (first/last frame) or ``"reference"``
    (3+ reference images). ``image_url`` merges with
    ``reference_image_urls`` as the first input. Image-to-video and
    reference-to-video are mutually exclusive on the API, so 3+ images
    switches the whole request to reference mode.
    """
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]

    candidates: List[str] = []
    if image_url and image_url.strip():
        candidates.append(image_url.strip())
    for ref in reference_image_urls or []:
        if ref and ref.strip():
            candidates.append(ref.strip())

    normalized: List[str] = []  # normalized image URLs (only valid inputs)
    for cand in candidates:
        img = _build_image_input(cand)
        if img and img.get("url"):
            normalized.append(img["url"])

    if not normalized:
        return content, "text"

    if len(normalized) == 1:
        content.append(_build_h3_image_item(normalized[0], "first_frame"))
        return content, "image"

    if len(normalized) == 2:
        content.append(_build_h3_image_item(normalized[0], "first_frame"))
        content.append(_build_h3_image_item(normalized[1], "last_frame"))
        return content, "image"

    # 3+ images → reference-to-video; all images become references.
    for url in normalized[:H3_MAX_REFERENCE_IMAGES]:
        content.append(_build_h3_image_item(url, "reference_image"))
    return content, "reference"


def _resolve_h3_ratio(
    mode: str,
    aspect_ratio: Optional[str],
) -> Tuple[str, Optional[str]]:
    """Return ``(ratio, adjustment_note)`` for the H3 v2 API.

    Image / reference modes: ratio is always ``adaptive`` (the API derives
    it from the input; concrete values are ignored).
    Text mode: ratio is required and cannot be ``adaptive`` — map the
    unified-surface value onto the v2 enum.
    """
    if mode != "text":
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
    duration: Optional[int],
    aspect_ratio: Optional[str],
) -> Tuple[Dict[str, Any], Optional[str], str]:
    """Build the H3 v2 request body (pure; no I/O).

    Returns ``(payload, ratio_note, mode)``. Raises ``ValueError`` for
    invalid prompts (empty, over the 7000-char limit).
    """
    if not prompt_text or not prompt_text.strip():
        raise ValueError("prompt is required for MiniMax H3 video generation.")

    if len(prompt_text) > H3_MAX_TEXT_CHARS:
        raise ValueError(
            f"MiniMax H3 text prompt exceeds the {H3_MAX_TEXT_CHARS}-character "
            f"limit ({len(prompt_text)} chars). Shorten the prompt."
        )

    content, mode = _build_h3_content(prompt_text, image_url, reference_image_urls)

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
        }

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
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
            return _run_coroutine(
                _generate_h3_video_async(
                    prompt=prompt,
                    image_url=image_url,
                    reference_image_urls=reference_image_urls,
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
        # MiniMax Hailuo 2.3 / other v1 models → legacy v1 API
        return _run_coroutine(
            _generate_minimax_video_async(
                prompt=prompt,
                model=resolved_model,
                image_url=image_url,
                reference_image_urls=reference_image_urls,
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
