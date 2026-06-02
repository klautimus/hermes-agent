#!/usr/bin/env python3
"""Trafilatura extract provider — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`.
Local-only extraction — no API key or external service needed.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


class TrafilaturaExtractProvider(WebSearchProvider):

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
        results: List[Dict[str, Any]] = []

        for url in urls:
            try:
                downloaded = trafilatura.fetch_url(url)
                if not downloaded:
                    results.append({
                        "url": url, "title": "", "content": "", "raw_content": "",
                        "metadata": {},
                        "error": "Trafilatura: page returned empty content (may be JS-only or paywalled)",
                    })
                    continue

                content = trafilatura.extract(
                    downloaded,
                    output_format=output_format,
                    include_comments=False,
                    include_tables=True,
                    no_fallback=False,
                )

                if not content:
                    results.append({
                        "url": url, "title": "", "content": "", "raw_content": "",
                        "metadata": {},
                        "error": "Trafilatura: could not extract meaningful content",
                    })
                    continue

                # Extract metadata
                metadata: Dict[str, Any] = {}
                title = ""
                try:
                    metadata_raw = trafilatura.extract(
                        downloaded, output_format="json", include_comments=False,
                    )
                    if metadata_raw:
                        import json
                        meta_obj = json.loads(metadata_raw)
                        title = meta_obj.get("title", "")
                        metadata = {k: v for k, v in meta_obj.items() if k not in ("text", "raw_text")}
                except Exception:
                    pass

                results.append({
                    "url": url, "title": title, "content": content,
                    "raw_content": content, "metadata": metadata,
                })
            except Exception as exc:
                results.append({
                    "url": url, "title": "", "content": "", "raw_content": "",
                    "metadata": {}, "error": f"Trafilatura failed: {exc}",
                })

        return results
