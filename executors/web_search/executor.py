#!/usr/bin/env python3
"""Web search executor - searches the web using Brave Search API.

Requires BRAVE_API_KEY environment variable.
Takes query (string) and optional count (int, default 5) parameters.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Any

import requests

BRAVE_BASE_URL = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_COUNT = 5


def search_web(query: str, count: int = DEFAULT_COUNT) -> dict:
    """Search the web using Brave Search API."""
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        raise ValueError("BRAVE_API_KEY environment variable is required")

    headers = {
        "X-Subscription-Token": api_key,
        "Accept": "application/json"
    }
    
    params = {
        "q": query,
        "count": min(count, 20)  # Brave API limits to 20 results
    }

    try:
        response = requests.get(BRAVE_BASE_URL, headers=headers, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()
        
        # Extract web results
        web_results = data.get("web", {}).get("results", [])
        
        results = []
        for result in web_results:
            results.append({
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "snippet": result.get("description", ""),
                "age": result.get("age", ""),
                "language": result.get("language", ""),
            })
        
        return {
            "query": query,
            "count_requested": count,
            "count_returned": len(results),
            "results": results
        }
        
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Web search request failed: {e}") from e
    except KeyError as e:
        raise RuntimeError(f"Unexpected API response format: missing {e}") from e


def main() -> None:
    query = os.environ.get("QUERY", "")
    count_str = os.environ.get("COUNT", str(DEFAULT_COUNT))
    
    # Also accept as CLI args (for testing)
    if len(sys.argv) > 1:
        query = sys.argv[1]
    if len(sys.argv) > 2:
        count_str = sys.argv[2]
    
    if not query:
        print(json.dumps({"error": "Query is required"}), file=sys.stderr)
        sys.exit(1)
    
    try:
        count = int(count_str)
        if count <= 0:
            count = DEFAULT_COUNT
    except (ValueError, TypeError):
        count = DEFAULT_COUNT

    try:
        result = search_web(query, count)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()