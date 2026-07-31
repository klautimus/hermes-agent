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
