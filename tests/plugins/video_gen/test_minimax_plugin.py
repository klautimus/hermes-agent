"""Smoke + unit tests for the MiniMax video gen plugin (H3 v2 + Hailuo 2.3 v1)."""

from __future__ import annotations

import pytest

from agent import video_gen_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    video_gen_registry._reset_for_tests()
    yield
    video_gen_registry._reset_for_tests()


# ---------------------------------------------------------------------------
# Provider surface
# ---------------------------------------------------------------------------


def test_minimax_provider_registers():
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    provider = MinimaxVideoGenProvider()
    video_gen_registry.register_provider(provider)

    assert video_gen_registry.get_provider("minimax") is provider
    assert provider.display_name == "MiniMax"
    assert provider.default_model() == "MiniMax-H3"


def test_minimax_provider_lists_models():
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    models = MinimaxVideoGenProvider().list_models()
    ids = [model["id"] for model in models]

    assert "MiniMax-H3" in ids
    assert "MiniMax-Hailuo-2.3" in ids
    # H3 is the flagship / first entry
    assert models[0]["id"] == "MiniMax-H3"
    assert models[0]["modalities"] == ["text", "image"]


def test_minimax_h3_capabilities():
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    caps = MinimaxVideoGenProvider().capabilities()
    assert caps["modalities"] == ["text", "image"]
    assert caps["resolutions"] == ["2K"]
    assert "21:9" in caps["aspect_ratios"]
    assert "16:9" in caps["aspect_ratios"]
    assert caps["min_duration"] == 4
    assert caps["max_duration"] == 15
    assert caps["max_reference_images"] == 9
    assert caps["supports_audio"] is False
    assert caps["supports_negative_prompt"] is False


def test_minimax_hailuo_capabilities():
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    caps = MinimaxVideoGenProvider().capabilities(model="MiniMax-Hailuo-2.3")
    assert set(caps["resolutions"]) == {"768P", "1080P"}
    assert caps["min_duration"] == 6
    assert caps["max_duration"] == 10
    assert caps["max_reference_images"] == 0
    assert "21:9" not in caps["aspect_ratios"]


def test_minimax_unavailable_without_key(monkeypatch):
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_HOST", raising=False)
    assert MinimaxVideoGenProvider().is_available() is False


def test_minimax_generate_requires_api_key(monkeypatch):
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setenv("MINIMAX_API_HOST", "https://api.minimax.io")
    result = MinimaxVideoGenProvider().generate("a happy dog")
    assert result["success"] is False
    assert result["error_type"] == "auth_required"


def test_minimax_validates_prompt(monkeypatch):
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setenv("MINIMAX_API_HOST", "https://api.minimax.io")
    result = MinimaxVideoGenProvider().generate("")
    assert result["success"] is False
    assert result["error_type"] == "missing_prompt"


def test_minimax_no_operation_kwarg():
    """The ABC's generate() signature must accept 'operation' via **kwargs
    (forward-compat). It should NOT TypeError."""
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    # Will fail with auth_required (no real key), but must NOT
    # fail with TypeError about unexpected keyword argument.
    result = MinimaxVideoGenProvider().generate("x", operation="generate")
    assert result["success"] is False
    assert result["error_type"] in {"auth_required", "api_error"}


def test_minimax_resolve_credentials_from_env(monkeypatch):
    from plugins.video_gen.minimax import _resolve_minimax_credentials

    monkeypatch.setenv("MINIMAX_API_KEY", "my-secret-key")
    monkeypatch.setenv("MINIMAX_API_HOST", "https://api.minimax.io")
    api_key, base_url = _resolve_minimax_credentials()
    assert api_key == "my-secret-key"
    assert base_url == "https://api.minimax.io"


def test_minimax_resolve_credentials_defaults_base_url(monkeypatch):
    from plugins.video_gen.minimax import _resolve_minimax_credentials

    monkeypatch.setenv("MINIMAX_API_KEY", "my-secret-key")
    monkeypatch.delenv("MINIMAX_API_HOST", raising=False)
    _api_key, base_url = _resolve_minimax_credentials()
    assert base_url == "https://api.minimax.io"


def test_minimax_build_image_input_from_https():
    from plugins.video_gen.minimax import _build_image_input

    result = _build_image_input("https://example.com/image.jpg")
    assert result == {"url": "https://example.com/image.jpg"}


def test_minimax_build_image_input_from_data_uri():
    from plugins.video_gen.minimax import _build_image_input

    result = _build_image_input("data:image/png;base64,YWJj")
    assert result == {"url": "data:image/png;base64,YWJj"}


def test_minimax_build_image_input_unknown_is_none():
    from plugins.video_gen.minimax import _build_image_input

    # Non-file, non-URL string → None
    assert _build_image_input("") is None
    assert _build_image_input("not-a-url-or-file") is None


def test_minimax_no_extra_keys_in_payload():
    """generate() must not TypeError when the ABC passes extra unknown kwargs."""
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    result = MinimaxVideoGenProvider().generate(
        "a dog",
        _model_override_explicit=True,
        some_future_param="ignored",
    )
    # Must not raise TypeError — should get through to auth/error check
    assert result["success"] is False


# ---------------------------------------------------------------------------
# H3 v2 payload builder (pure functions, no I/O)
# ---------------------------------------------------------------------------


def test_h3_payload_text_to_video():
    from plugins.video_gen.minimax import _build_h3_payload

    payload, ratio_note, mode = _build_h3_payload(
        prompt_text="A cat in space",
        image_url=None,
        reference_image_urls=None,
        duration=None,
        aspect_ratio=None,
    )
    assert payload["model"] == "MiniMax-H3"
    assert payload["resolution"] == "2K"
    assert payload["duration"] == 6
    assert payload["ratio"] == "16:9"
    assert payload["content"] == [{"type": "text", "text": "A cat in space"}]
    assert mode == "text"
    assert ratio_note is None


def test_h3_payload_t2v_ratio_mapping():
    from plugins.video_gen.minimax import _build_h3_payload

    payload, ratio_note, _ = _build_h3_payload(
        prompt_text="x", image_url=None, reference_image_urls=None,
        duration=None, aspect_ratio="3:2",
    )
    assert payload["ratio"] == "16:9"
    assert ratio_note == "3:2->16:9"

    payload, ratio_note, _ = _build_h3_payload(
        prompt_text="x", image_url=None, reference_image_urls=None,
        duration=None, aspect_ratio="2:3",
    )
    assert payload["ratio"] == "9:16"
    assert ratio_note == "2:3->9:16"

    payload, ratio_note, _ = _build_h3_payload(
        prompt_text="x", image_url=None, reference_image_urls=None,
        duration=None, aspect_ratio="9:16",
    )
    assert payload["ratio"] == "9:16"
    assert ratio_note is None


def test_h3_payload_t2v_adaptive_guard():
    """t2v must never send ratio=adaptive (invalid on the v2 API)."""
    from plugins.video_gen.minimax import _build_h3_payload

    payload, ratio_note, _ = _build_h3_payload(
        prompt_text="x", image_url=None, reference_image_urls=None,
        duration=None, aspect_ratio="adaptive",
    )
    assert payload["ratio"] == "16:9"
    assert ratio_note == "adaptive->16:9"


def test_h3_payload_i2v_single_image():
    from plugins.video_gen.minimax import _build_h3_payload

    payload, ratio_note, mode = _build_h3_payload(
        prompt_text="animate this",
        image_url="https://example.com/frame.jpg",
        reference_image_urls=None,
        duration=None,
        aspect_ratio="16:9",
    )
    assert mode == "image"
    assert payload["ratio"] == "adaptive"
    assert ratio_note is None
    assert payload["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/frame.jpg"},
        "role": "first_frame",
    }


def test_h3_payload_i2v_first_and_last_frame():
    from plugins.video_gen.minimax import _build_h3_payload

    payload, _note, mode = _build_h3_payload(
        prompt_text="transition",
        image_url=None,
        reference_image_urls=[
            "https://example.com/start.jpg",
            "https://example.com/end.jpg",
        ],
        duration=None,
        aspect_ratio=None,
    )
    assert mode == "image"
    assert payload["ratio"] == "adaptive"
    roles = [item.get("role") for item in payload["content"]]
    assert roles == [None, "first_frame", "last_frame"]


def test_h3_payload_image_url_merges_with_refs():
    """image_url merges as the first input with reference_image_urls."""
    from plugins.video_gen.minimax import _build_h3_payload

    payload, _note, mode = _build_h3_payload(
        prompt_text="merge",
        image_url="https://example.com/a.jpg",
        reference_image_urls=["https://example.com/b.jpg"],
        duration=None,
        aspect_ratio=None,
    )
    assert mode == "image"
    roles = [item.get("role") for item in payload["content"]]
    assert roles == [None, "first_frame", "last_frame"]


def test_h3_payload_reference_mode_three_plus():
    from plugins.video_gen.minimax import _build_h3_payload

    payload, _note, mode = _build_h3_payload(
        prompt_text="style transfer",
        image_url=None,
        reference_image_urls=[
            "https://example.com/r1.jpg",
            "https://example.com/r2.jpg",
            "https://example.com/r3.jpg",
        ],
        duration=None,
        aspect_ratio=None,
    )
    assert mode == "reference"
    assert payload["ratio"] == "adaptive"
    roles = [item.get("role") for item in payload["content"] if item["type"] != "text"]
    assert roles == ["reference_image", "reference_image", "reference_image"]


def test_h3_payload_duration_clamp():
    from plugins.video_gen.minimax import _build_h3_payload

    def dur_for(value):
        payload, _note, _mode = _build_h3_payload(
            prompt_text="x", image_url=None, reference_image_urls=None,
            duration=value, aspect_ratio=None,
        )
        return payload["duration"]

    assert dur_for(None) == 6
    assert dur_for(3) == 4
    assert dur_for(20) == 15
    assert dur_for(10) == 10


def test_h3_payload_empty_prompt_raises():
    from plugins.video_gen.minimax import _build_h3_payload

    with pytest.raises(ValueError):
        _build_h3_payload(
            prompt_text="   ", image_url=None, reference_image_urls=None,
            duration=None, aspect_ratio=None,
        )


def test_h3_payload_prompt_too_long_raises():
    from plugins.video_gen.minimax import _build_h3_payload

    with pytest.raises(ValueError):
        _build_h3_payload(
            prompt_text="x" * 7001, image_url=None, reference_image_urls=None,
            duration=None, aspect_ratio=None,
        )


# ---------------------------------------------------------------------------
# H3 v2 query result parsing
# ---------------------------------------------------------------------------


def test_h3_result_succeeded_with_url():
    from plugins.video_gen.minimax import _extract_h3_result

    status, url, err = _extract_h3_result({
        "task": {
            "status": "succeeded",
            "content": {"url": "https://cdn.hailuoai.com/out.mp4"},
            "ratio": "16:9",
        }
    })
    assert status == "succeeded"
    assert url == "https://cdn.hailuoai.com/out.mp4"
    assert err is None


def test_h3_result_failed_with_error_message():
    from plugins.video_gen.minimax import _extract_h3_result

    status, url, err = _extract_h3_result({
        "task": {
            "status": "failed",
            "error": {"code": 1026, "message": "video description contains sensitive content"},
        }
    })
    assert status == "failed"
    assert url is None
    assert err is not None and "sensitive content" in err


def test_h3_result_content_list_fallback():
    """Defensive: some responses may wrap content in a list."""
    from plugins.video_gen.minimax import _extract_h3_result

    status, url, _err = _extract_h3_result({
        "task": {"status": "succeeded", "content": [{"url": "https://x/y.mp4"}]}
    })
    assert status == "succeeded"
    assert url == "https://x/y.mp4"


def test_minimax_error_message_envelope():
    from plugins.video_gen.minimax import _minimax_error_message

    msg = _minimax_error_message(
        '{"type":"error","error":{"type":"insufficient_balance_error",'
        '"message":"insufficient balance (1008)","http_code":402},'
        '"request_id":"abc"}',
        "fallback",
    )
    assert "insufficient balance" in msg

    assert _minimax_error_message("not json at all", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# H3 model routing + regression guards
# ---------------------------------------------------------------------------


def test_h3_capabilities_no_args_matches_default_model():
    """The tool calls capabilities() with NO args — the no-arg default must
    be the H3 surface (the active default model), not the v1 surface."""
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    caps = MinimaxVideoGenProvider().capabilities()
    assert caps["resolutions"] == ["2K"]
    assert caps["min_duration"] == 4
    assert caps["max_duration"] == 15
    assert caps["max_reference_images"] == 9


def test_is_h3_model_exact_match():
    from plugins.video_gen.minimax import _is_h3_model

    assert _is_h3_model("MiniMax-H3") is True
    assert _is_h3_model("minimax-h3") is True
    assert _is_h3_model("h3") is True
    assert _is_h3_model("MiniMax-Hailuo-2.3") is False
    assert _is_h3_model("h30") is False  # substring footgun guard
    assert _is_h3_model("h3b") is False
    assert _is_h3_model("") is False
    assert _is_h3_model(None) is False


def test_h3_payload_reference_mode_ratio_passthrough():
    """r2v allows an explicit concrete ratio per the docs (optional, defaults
    adaptive); a valid v2 ratio should pass through, an unsupported one
    should fall back to adaptive."""
    from plugins.video_gen.minimax import _build_h3_payload

    payload, note, mode = _build_h3_payload(
        prompt_text="style",
        image_url=None,
        reference_image_urls=[
            "https://example.com/r1.jpg",
            "https://example.com/r2.jpg",
            "https://example.com/r3.jpg",
        ],
        duration=None,
        aspect_ratio="9:16",
    )
    assert mode == "reference"
    assert payload["ratio"] == "9:16"
    assert note is None

    payload, note, _mode = _build_h3_payload(
        prompt_text="style",
        image_url=None,
        reference_image_urls=[
            "https://example.com/r1.jpg",
            "https://example.com/r2.jpg",
            "https://example.com/r3.jpg",
        ],
        duration=None,
        aspect_ratio="3:2",  # not in the v2 enum → adaptive fallback
    )
    assert payload["ratio"] == "adaptive"
    assert note is None


# ---------------------------------------------------------------------------
# H3 end-to-end generate() with mocked HTTP (submit → poll → download → save)
# ---------------------------------------------------------------------------


def test_h3_generate_full_flow_with_mocked_http(monkeypatch):
    import plugins.video_gen.minimax as mm

    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setenv("MINIMAX_API_HOST", "https://api.minimax.io")

    captured = {}

    async def _submit_impl(client, payload, *, api_key, base_url):
        captured["payload"] = payload
        captured["url"] = f"{base_url}/v2/video_generation"
        return "task-123"

    async def _poll_impl(client, task_id, *, api_key, base_url, timeout_seconds, poll_interval):
        captured["task_id"] = task_id
        captured["poll_url"] = f"{base_url}/v2/query/video_generation/{task_id}"
        return {
            "status": "succeeded",
            "body": {
                "task": {
                    "status": "succeeded",
                    "content": {"url": "https://cdn.example/out.mp4"},
                    "task_type": "generation",
                    "ratio": "16:9",
                }
            },
        }

    async def _fetch_impl(client, url, timeout=120):
        captured["download_url"] = url
        return b"VIDEOBINARYDATA"

    from pathlib import Path

    monkeypatch.setattr(mm, "_submit_h3_task", _submit_impl)
    monkeypatch.setattr(mm, "_poll_h3_task", _poll_impl)
    monkeypatch.setattr(mm, "_fetch_video_bytes", _fetch_impl)
    monkeypatch.setattr(mm, "save_bytes_video", lambda raw, prefix, extension: Path("/tmp/test-video.mp4"))

    result = mm.MinimaxVideoGenProvider().generate(
        "a test video",
        model="MiniMax-H3",
        seed=42,
        resolution="1080P",
        duration=10,
        aspect_ratio="16:9",
        reference_video_urls=["https://example.com/ref.mp4"],
    )

    assert result["success"] is True
    assert result["video"] == "/tmp/test-video.mp4"
    assert result["model"] == "MiniMax-H3"
    assert result["modality"] == "image"  # reference mode counts as image-driven
    # success_response() flattens `extra` items into the top-level dict
    assert result["api_version"] == "v2"
    assert result["task_id"] == "task-123"
    assert result["seed_dropped"] is True
    assert result["resolution_requested"] == "1080P"
    assert result["actual_ratio"] == "16:9"
    assert captured["url"].endswith("/v2/video_generation")
    assert captured["poll_url"].endswith("/v2/query/video_generation/task-123")
    assert captured["payload"]["model"] == "MiniMax-H3"
    assert captured["payload"]["resolution"] == "2K"
    assert captured["payload"]["duration"] == 10
    assert "seed" not in captured["payload"]
    assert captured["download_url"] == "https://cdn.example/out.mp4"
    # reference video reached the wire as a v2 content item
    non_text = [i for i in captured["payload"]["content"] if i["type"] != "text"]
    assert non_text == [
        {"type": "video_url", "video_url": {"url": "https://example.com/ref.mp4"}, "role": "reference_video"}
    ]


def test_h3_generate_body_size_guard(monkeypatch):
    import plugins.video_gen.minimax as mm

    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    # Tiny cap so any real payload trips the guard before network I/O.
    monkeypatch.setattr(mm, "H3_MAX_BODY_BYTES", 100)

    result = mm.MinimaxVideoGenProvider().generate(
        "a test video",
        model="MiniMax-H3",
        image_url="data:image/png;base64," + "A" * 500,
    )
    assert result["success"] is False
    assert result["error_type"] == "payload_too_large"


# ---------------------------------------------------------------------------
# Reference video / audio (v2 reference_video + reference_audio roles)
# ---------------------------------------------------------------------------


def test_build_media_input_urls_and_data_uris():
    from plugins.video_gen.minimax import _build_media_input

    assert _build_media_input("https://example.com/clip.mp4") == {"url": "https://example.com/clip.mp4"}
    assert _build_media_input("data:video/mp4;base64,YWJj") == {"url": "data:video/mp4;base64,YWJj"}
    assert _build_media_input("data:audio/mp3;base64,YWJj") == {"url": "data:audio/mp3;base64,YWJj"}
    assert _build_media_input("") is None
    assert _build_media_input("not-a-url-or-file") is None
    # image data URIs are NOT accepted by the media helper (images use _build_image_input)
    assert _build_media_input("data:image/png;base64,YWJj") is None


def test_h3_payload_reference_video_mode():
    from plugins.video_gen.minimax import _build_h3_payload

    payload, note, mode = _build_h3_payload(
        prompt_text="match this motion",
        image_url=None,
        reference_image_urls=None,
        reference_video_urls=["https://example.com/motion.mp4"],
        reference_audio_urls=None,
        duration=None,
        aspect_ratio=None,
    )
    assert mode == "reference"
    assert payload["ratio"] == "adaptive"
    items = [i for i in payload["content"] if i["type"] != "text"]
    assert items == [
        {"type": "video_url", "video_url": {"url": "https://example.com/motion.mp4"}, "role": "reference_video"}
    ]
    assert note is None


def test_h3_payload_reference_video_audio_and_images_mixed():
    """Images + video + audio refs together → all become reference roles."""
    from plugins.video_gen.minimax import _build_h3_payload

    payload, _note, mode = _build_h3_payload(
        prompt_text="full reference stack",
        image_url="https://example.com/style.jpg",
        reference_image_urls=["https://example.com/char.jpg"],
        reference_video_urls=[
            "https://example.com/a.mp4",
            "https://example.com/b.mp4",
            "https://example.com/c.mp4",
        ],
        reference_audio_urls=["https://example.com/vocals.wav"],
        duration=None,
        aspect_ratio="16:9",
    )
    assert mode == "reference"
    assert payload["ratio"] == "16:9"  # r2v honors an explicit concrete ratio
    items = [i for i in payload["content"] if i["type"] != "text"]
    roles = [i.get("role") for i in items]
    assert roles == [
        "reference_image",
        "reference_image",
        "reference_video",
        "reference_video",
        "reference_video",
        "reference_audio",
    ]
    # counts capped at API limits
    assert len([i for i in items if i.get("role") == "reference_video"]) == 3
    assert len([i for i in items if i.get("role") == "reference_audio"]) == 1
    assert len([i for i in items if i.get("role") == "reference_image"]) == 2


def test_h3_payload_reference_video_cap_three():
    """More than 3 reference videos are clamped to the API cap."""
    from plugins.video_gen.minimax import _build_h3_payload

    payload, _note, mode = _build_h3_payload(
        prompt_text="clamp",
        image_url=None,
        reference_image_urls=None,
        reference_video_urls=[
            f"https://example.com/v{i}.mp4" for i in range(6)
        ],
        reference_audio_urls=None,
        duration=None,
        aspect_ratio=None,
    )
    assert mode == "reference"
    videos = [i for i in payload["content"] if i.get("role") == "reference_video"]
    assert len(videos) == 3


def test_h3_payload_audio_alone_raises():
    """Audio alone is not a valid reference mode — must pair with image/video."""
    from plugins.video_gen.minimax import _build_h3_payload

    with pytest.raises(ValueError):
        _build_h3_payload(
            prompt_text="audio only",
            image_url=None,
            reference_image_urls=None,
            reference_video_urls=None,
            reference_audio_urls=["https://example.com/vocals.wav"],
            duration=None,
            aspect_ratio=None,
        )


def test_h3_capabilities_include_reference_video_audio():
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    caps = MinimaxVideoGenProvider().capabilities()
    assert caps["max_reference_videos"] == 3
    assert caps["max_reference_audio"] == 3

    v1_caps = MinimaxVideoGenProvider().capabilities(model="MiniMax-Hailuo-2.3")
    assert v1_caps["max_reference_videos"] == 0
    assert v1_caps["max_reference_audio"] == 0


def test_hailuo_v1_rejects_reference_video_audio(monkeypatch):
    """v1 has no reference video/audio roles — must fail loudly, not silently."""
    from plugins.video_gen.minimax import MinimaxVideoGenProvider

    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setenv("MINIMAX_API_HOST", "https://api.minimax.io")
    result = MinimaxVideoGenProvider().generate(
        "a dog",
        model="MiniMax-Hailuo-2.3",
        reference_video_urls=["https://example.com/clip.mp4"],
    )
    assert result["success"] is False
    assert result["error_type"] == "invalid_params"
    assert "MiniMax-H3" in result["error"]
