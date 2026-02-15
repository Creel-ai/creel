#!/usr/bin/env python3
"""Web fetch executor - fetches URLs and converts HTML to clean text/markdown.

Takes URL and optional max_chars parameters.
Outputs clean text extracted from HTML to stdout.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify


def clean_html_to_text(html: str, max_chars: int = 10000) -> str:
    """Convert HTML to clean text by stripping unwanted elements and extracting content.

    Args:
        html: Raw HTML content
        max_chars: Maximum characters to return

    Returns:
        Clean text content, truncated to max_chars if needed
    """
    soup = BeautifulSoup(html, 'html.parser')
    
    # Remove unwanted elements
    for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'noscript']):
        element.decompose()
    
    # Remove elements with certain classes/ids commonly used for navigation/ads
    unwanted_selectors = [
        '[class*="nav"]', '[class*="menu"]', '[class*="sidebar"]',
        '[class*="ad"]', '[class*="advertisement"]', '[class*="cookie"]',
        '[class*="popup"]', '[class*="modal"]', '[class*="social"]',
        '[id*="nav"]', '[id*="menu"]', '[id*="sidebar"]',
        '[id*="ad"]', '[id*="advertisement"]', '[id*="cookie"]'
    ]
    
    for selector in unwanted_selectors:
        for element in soup.select(selector):
            element.decompose()
    
    # Convert to markdown for better formatting
    markdown = markdownify(str(soup), heading_style="ATX", strip=['a'])
    
    # Clean up the markdown text
    lines = []
    for line in markdown.split('\n'):
        line = line.strip()
        if line and not line.startswith('#' * 6):  # Skip excessive headers
            lines.append(line)
    
    text = '\n'.join(lines)
    
    # Remove excessive whitespace
    while '\n\n\n' in text:
        text = text.replace('\n\n\n', '\n\n')
    
    # Truncate if needed
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(' ', 1)[0] + '...'
    
    return text.strip()


def fetch_url(url: str, max_chars: int = 10000) -> dict[str, Any]:
    """Fetch a URL and convert to clean text.

    Args:
        url: URL to fetch
        max_chars: Maximum characters to return in content

    Returns:
        Dict with url, title, content, and metadata
    """
    if not url or not url.startswith(('http://', 'https://')):
        raise ValueError(f"Invalid URL: {url}")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        response.raise_for_status()
        
        # Check content type
        content_type = response.headers.get('content-type', '').lower()
        if not any(ct in content_type for ct in ['text/html', 'application/xhtml', 'text/plain']):
            return {
                'url': url,
                'title': '',
                'content': f'[Error: Content type {content_type} not supported - only HTML content can be processed]',
                'status_code': response.status_code,
                'content_type': content_type,
                'size': len(response.content),
                'error': f'Unsupported content type: {content_type}'
            }
        
        # Extract title
        soup = BeautifulSoup(response.text, 'html.parser')
        title_element = soup.find('title')
        title = title_element.get_text().strip() if title_element else ''
        
        # Convert to clean text
        content = clean_html_to_text(response.text, max_chars)
        
        return {
            'url': url,
            'title': title,
            'content': content,
            'status_code': response.status_code,
            'content_type': content_type,
            'size': len(content),
        }
        
    except requests.exceptions.Timeout:
        return {
            'url': url,
            'title': '',
            'content': '[Error: Request timeout after 30 seconds]',
            'status_code': 0,
            'content_type': '',
            'size': 0,
            'error': 'Request timeout'
        }
    except requests.exceptions.ConnectionError:
        return {
            'url': url,
            'title': '',
            'content': '[Error: Connection failed - could not reach the server]',
            'status_code': 0,
            'content_type': '',
            'size': 0,
            'error': 'Connection error'
        }
    except requests.exceptions.HTTPError as e:
        return {
            'url': url,
            'title': '',
            'content': f'[Error: HTTP {e.response.status_code}]',
            'status_code': e.response.status_code if e.response else 0,
            'content_type': '',
            'size': 0,
            'error': f'HTTP error: {e}'
        }
    except Exception as e:
        return {
            'url': url,
            'title': '',
            'content': f'[Error: {str(e)}]',
            'status_code': 0,
            'content_type': '',
            'size': 0,
            'error': str(e)
        }


def main() -> None:
    """Main entry point for the web fetch executor."""
    url = os.environ.get("URL", "")
    max_chars = int(os.environ.get("MAX_CHARS", "10000"))

    # CLI arg overrides
    if len(sys.argv) > 1:
        url = sys.argv[1]
    if len(sys.argv) > 2:
        max_chars = int(sys.argv[2])

    if not url:
        print(json.dumps({"error": "URL parameter is required"}), file=sys.stderr)
        sys.exit(1)

    try:
        result = fetch_url(url, max_chars)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()