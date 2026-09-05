#!/usr/bin/env python3
"""Trafilatura extract provider — plugin form.

Extract-only provider. Uses the trafilatura Python package to fetch
and extract readable content from arbitrary URLs. Returns markdown
suitable for LLM consumption.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


class TrafilaturaExtractProvider(WebSearchProvider):
    """Extract web content using the trafilatura Python package."""

    @property
    def name(self) -> str:
        return "trafilatura"

    @property
    def display_name(self) -> str:
        return "Trafilatura"

    def is_available(self) -> bool:
        try:
            import trafilatura  # noqa: F401
            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        import trafilatura

        output_format = kwargs.get("format", "markdown")
        if output_format == "html":
            output_format = "markdown"

        results: List[Dict[str, Any]] = []

        for url in urls:
            try:
                downloaded = trafilatura.fetch_url(url)
                if not downloaded:
                    results.append({
                        "url": url, "title": "", "content": "",
                        "raw_content": "", "metadata": {},
                        "error": "Trafilatura: page returned empty content",
                    })
                    continue

                content = trafilatura.extract(
                    downloaded, output_format=output_format,
                    include_comments=False, include_tables=True,
                    no_fallback=False,
                )
                if not content:
                    results.append({
                        "url": url, "title": "", "content": "",
                        "raw_content": "", "metadata": {},
                        "error": "Trafilatura: could not extract meaningful content",
                    })
                    continue

                title = ""
                metadata: Dict[str, Any] = {}
                try:
                    meta_raw = trafilatura.extract(
                        downloaded, output_format="json",
                        include_comments=False,
                    )
                    if meta_raw:
                        meta_obj = json.loads(meta_raw)
                        title = meta_obj.get("title", "")
                        metadata = {
                            k: v for k, v in meta_obj.items()
                            if k not in ("text", "raw_text")
                        }
                except Exception:
                    pass

                results.append({
                    "url": url, "title": title, "content": content,
                    "raw_content": content, "metadata": metadata,
                })
            except Exception as exc:
                logger.debug("Trafilatura failed for %s: %s", url, exc)
                results.append({
                    "url": url, "title": "", "content": "",
                    "raw_content": "", "metadata": {},
                    "error": f"Trafilatura failed: {exc}",
                })

        return results
