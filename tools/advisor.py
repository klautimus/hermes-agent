"""Advisor tool — on-demand consultation with a secondary model for decisions, plans, and complex research.

The executor (main model) calls this tool when it needs guidance from the advisor model.
This implements the Advisor pattern: executor runs the main loop, consults advisor on-demand.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tools.registry import registry

logger = logging.getLogger(__name__)

# Default system prompt for the advisor model
_ADVISOR_SYSTEM_PROMPT = (
    "You are the strategic brain of a two-model agent system. The executor (a separate model) "
    "handles all tool calls, file operations, code changes, and user interaction. YOUR job is "
    "to do ALL the thinking: analysis, planning, architecture decisions, debugging strategy, "
    "design choices, and task decomposition.\n\n"
    "The executor will call you with a question and context. You must:\n"
    "1. Analyze the situation deeply — understand the goal, constraints, and current state\n"
    "2. Formulate a clear strategy or decision — be decisive, not hedging\n"
    "3. Provide concrete, actionable guidance — specific next steps, not vague suggestions\n"
    "4. Identify risks, pitfalls, and failure modes the executor should watch for\n"
    "5. If the task is complex, break it into ordered steps the executor can follow\n\n"
    "You do NOT execute anything, call tools, access files, browse, or take actions. "
    "The executor holds those capabilities and will act on your guidance.\n\n"
    "The conversation below contains the executor's question and any context they've provided. "
    "Give your most intelligent analysis. Lead with your conclusion/decision, then supporting reasoning. "
    "Be direct. No preamble, no disclaimers about tools or access. "
    "Your response is private guidance handed to the executor, not an answer shown to the user."
)


def _check_advisor_available() -> bool:
    """Check if the advisor auxiliary task is configured."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
        auxiliary = config.get("auxiliary", {})
        advisor = auxiliary.get("advisor", {})
        provider = advisor.get("provider", "")
        model = advisor.get("model", "")
        # Advisor is available if provider is configured
        return bool(provider)
    except Exception:
        return False


def _get_advisor_config() -> dict:
    """Get the advisor configuration from config.yaml."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
        auxiliary = config.get("auxiliary", {})
        return auxiliary.get("advisor", {})
    except Exception:
        return {}


def _get_advisor_system_prompt() -> str:
    """Get the advisor system prompt, allowing config override."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
        auxiliary = config.get("auxiliary", {})
        advisor = auxiliary.get("advisor", {})
        custom_prompt = advisor.get("system_prompt", "")
        if custom_prompt:
            return custom_prompt
    except Exception:
        pass
    return _ADVISOR_SYSTEM_PROMPT


def consult_advisor(question: str, context: str = "") -> str:
    """Consult the advisor model for guidance on a decision, plan, or complex question.

    Args:
        question: The specific question or decision you want guidance on. Be precise.
        context: Optional additional context — relevant files, code snippets, background info,
                 constraints, or anything else the advisor should know to give good advice.

    Returns:
        The advisor's analysis and guidance as a JSON string.
    """
    # Build the message for the advisor
    user_content = f"Question: {question}"
    if context:
        user_content += f"\n\nContext:\n{context}"

    messages = [
        {"role": "system", "content": _get_advisor_system_prompt()},
        {"role": "user", "content": user_content},
    ]

    # Get advisor config
    advisor_config = _get_advisor_config()
    provider = advisor_config.get("provider", "nvidia")
    model = advisor_config.get("model", "glm-5.2")
    temperature = advisor_config.get("temperature", 0.7)
    max_tokens = advisor_config.get("max_tokens")
    timeout = advisor_config.get("timeout", 120)
    extra_body = advisor_config.get("extra_body", {}) or {}
    visible = advisor_config.get("visible_advice", True)

    # max_tokens can be None (no cap) or int
    max_tokens_val = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else None

    # Retry logic for transient failures:
    #   - HTTP 429 rate limits (provider rate limit)
    #   - Timeout errors (provider slow, network blip)
    #   - Connection errors (transient network issue, momentary upstream outage)
    # Non-transient errors (4xx other than 429, auth failures, invalid model) fall
    # through immediately. On the final attempt (after retries are exhausted) or
    # for unparseable responses, fail hard and return error JSON — never silently
    # substitute `str(response)`, which would surface transport-object stringification
    # to the executor as if it were real advisor output.
    import time

    # Retry policy — tuned for providers (like Nvidia) that fail individual
    # requests but succeed if you keep trying:
    #   - 20s base delay between attempts (so we don't hammer a rate-limited provider)
    #   - Exponential backoff: 20s, 40s, 80s, 160s, ...
    #   - 5 attempts total → up to 5 minutes of fallback wall-clock per call
    #     (pure-backoff budget alone is 20+40+80+160 = 300s = 5min, not counting
    #     in-flight latency which is typically 5-15s per attempt)
    #   - This is a HEAVY retry — by design. Each advisor call is meant to be
    #     high-quality; we'd rather wait for one good answer than get a fast
    #     429-bounced failure.
    # Both values are also exposed via the advisor model config block so power
    # users can tighten them without code changes.
    max_retries = advisor_config.get("max_retries", 5)
    base_delay = advisor_config.get("retry_base_delay", 20.0)

    # Cache the openai exceptions at module-import cost. If the openai SDK is
    # older or absent, the retry classifier falls back to string matching only.
    try:
        from openai import RateLimitError, APITimeoutError, APIConnectionError
    except ImportError:
        RateLimitError = APITimeoutError = APIConnectionError = None

    # Build the tuple of transient exception classes, dropping Nones so isinstance
    # doesn't raise TypeError when a class is unavailable.
    transient_exc_classes = tuple(
        cls for cls in (RateLimitError, APITimeoutError, APIConnectionError) if cls is not None
    )

    def _is_transient(exc: Exception) -> bool:
        # Class-based check first — most reliable, no string parsing on the error.
        if transient_exc_classes and isinstance(exc, transient_exc_classes):
            return True
        # Fallback string match for providers that don't raise typed exceptions
        # or for older API client versions.
        msg = str(exc).lower()
        if "429" in msg or "rate limit" in msg:
            return True
        if "timeout" in msg or "timed out" in msg:
            return True
        if "connection" in msg and ("error" in msg or "reset" in msg or "refused" in msg):
            return True
        return False

    for attempt in range(max_retries):
        try:
            from agent.auxiliary_client import call_llm

            response = call_llm(
                task="advisor",
                provider=provider,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens_val,
                timeout=timeout,
                extra_body=extra_body,
            )

            # Parse the response. If we can't extract a coherent text payload,
            # fail hard — don't fake-success by stringifying the transport object,
            # which would have the executor reason over model-metadata strings.
            if hasattr(response, "choices") and response.choices:
                choice = response.choices[0]
                if hasattr(choice, "message") and choice.message:
                    content = getattr(choice.message, "content", None)
                    if content:
                        return json.dumps(
                            {
                                "success": True,
                                "advisor_response": content,
                                "provider": provider,
                                "model": model,
                                "visible": visible,
                            }
                        )

            # Unparseable response — log and return error JSON.
            logger.error(
                "Advisor returned an unparseable response from %s/%s: %r",
                provider, model, response,
            )
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        "Advisor provider returned a response with no extractable "
                        f"text content (model={model}, provider={provider}). "
                        "This is treated as a hard failure rather than a stringified "
                        "transport object — surface the error to the executor."
                    ),
                    "provider": provider,
                    "model": model,
                }
            )

        except Exception as exc:
            if _is_transient(exc) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Advisor transient failure (attempt %d/%d, %.1fs backoff): %s",
                    attempt + 1, max_retries, delay, exc,
                )
                time.sleep(delay)
                continue

            logger.exception("Advisor consultation failed")
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                    "provider": provider,
                    "model": model,
                }
            )


# Register the tool
registry.register(
    name="consult_advisor",
    toolset="advisor",
    schema={
        "name": "consult_advisor",
        "description": (
            "Consult the advisor model (the STRATEGIC BRAIN) for analysis, planning, and decisions. "
            "The advisor does ALL thinking: architecture, design, debugging strategy, task decomposition, "
            "prioritization. You are the EXECUTOR — provide context, get a plan, execute it. "
            "DEFAULT: consult the advisor for ANY non-trivial task, decision, or uncertainty. "
            "Pass a precise question and ALL relevant context (code, errors, constraints, goals). "
            "The advisor sees ONLY what you pass. Context budget: ~20KB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question, decision, or problem for the advisor to analyze. Be precise about what you're trying to achieve and what the constraints are.",
                },
                "context": {
                    "type": "string",
                    "description": "ALL relevant context: code snippets, error output, file contents, constraints, goals, prior attempts. The advisor has NO access to conversation history, tools, or files — only what you pass here. Err on the side of MORE context.",
                },
            },
            "required": ["question"],
        },
    },
    handler=lambda args, **kwargs: consult_advisor(
        question=args.get("question", ""),
        context=args.get("context", ""),
    ),
    check_fn=_check_advisor_available,
    requires_env=[],
    description="Strategic advisor: the brain that plans, decides, and directs; you execute",
    emoji="🧠",
)