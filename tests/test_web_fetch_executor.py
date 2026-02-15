#!/usr/bin/env python3
"""Tests for web_fetch executor."""

import json
from unittest.mock import Mock, patch

import pytest
import requests

from executors.web_fetch.executor import clean_html_to_text, fetch_url


def test_clean_html_to_text_basic():
    """Test basic HTML to text conversion."""
    html = """
    <html>
        <head><title>Test Page</title></head>
        <body>
            <h1>Main Heading</h1>
            <p>This is a paragraph with <a href="/link">a link</a>.</p>
            <script>alert('ads');</script>
            <style>body { color: red; }</style>
            <nav>Navigation</nav>
            <footer>Footer content</footer>
        </body>
    </html>
    """
    
    result = clean_html_to_text(html)
    
    # Should contain main content
    assert "Main Heading" in result
    assert "This is a paragraph with a link." in result
    
    # Should strip unwanted elements
    assert "alert('ads');" not in result
    assert "color: red;" not in result
    assert "Navigation" not in result
    assert "Footer content" not in result


def test_clean_html_to_text_truncation():
    """Test text truncation at max_chars."""
    html = "<html><body><p>" + "A" * 100 + " word boundary " + "B" * 100 + "</p></body></html>"
    
    result = clean_html_to_text(html, max_chars=50)
    
    # Should truncate at word boundary and add ellipsis
    assert len(result) <= 53  # 50 + "..."
    assert result.endswith("...")
    assert "word boundary" not in result  # Should break before this


def test_clean_html_to_text_unwanted_classes():
    """Test removal of elements with unwanted classes and IDs."""
    html = """
    <html>
        <body>
            <div class="content">Good content</div>
            <div class="sidebar-nav">Sidebar</div>
            <div id="advertisement">Ad content</div>
            <div class="social-share">Share</div>
            <div id="cookie-banner">Cookies</div>
        </body>
    </html>
    """
    
    result = clean_html_to_text(html)
    
    assert "Good content" in result
    assert "Sidebar" not in result
    assert "Ad content" not in result
    assert "Share" not in result
    assert "Cookies" not in result


@patch('requests.get')
def test_fetch_url_success(mock_get):
    """Test successful URL fetch and processing."""
    mock_response = Mock()
    mock_response.text = """
    <html>
        <head><title>Test Article</title></head>
        <body>
            <h1>Article Title</h1>
            <p>Article content here.</p>
        </body>
    </html>
    """
    mock_response.status_code = 200
    mock_response.headers = {'content-type': 'text/html; charset=utf-8'}
    mock_get.return_value = mock_response
    
    result = fetch_url("https://example.com/article")
    
    assert result['url'] == "https://example.com/article"
    assert result['title'] == "Test Article"
    assert "Article Title" in result['content']
    assert "Article content here." in result['content']
    assert result['status_code'] == 200
    assert result['content_type'] == 'text/html; charset=utf-8'
    assert 'error' not in result


@patch('requests.get')
def test_fetch_url_http_error(mock_get):
    """Test handling of HTTP errors (404, 500, etc.)."""
    mock_response = Mock()
    mock_response.status_code = 404
    mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_response)
    mock_get.return_value = mock_response
    
    result = fetch_url("https://example.com/notfound")
    
    assert result['url'] == "https://example.com/notfound"
    assert result['status_code'] == 404
    assert '[Error: HTTP 404]' in result['content']
    assert 'error' in result


@patch('requests.get')
def test_fetch_url_timeout(mock_get):
    """Test handling of request timeouts."""
    mock_get.side_effect = requests.exceptions.Timeout()
    
    result = fetch_url("https://slow.example.com")
    
    assert result['url'] == "https://slow.example.com"
    assert result['status_code'] == 0
    assert '[Error: Request timeout after 30 seconds]' in result['content']
    assert result['error'] == 'Request timeout'


@patch('requests.get')
def test_fetch_url_connection_error(mock_get):
    """Test handling of connection errors."""
    mock_get.side_effect = requests.exceptions.ConnectionError()
    
    result = fetch_url("https://nonexistent.example.com")
    
    assert result['url'] == "https://nonexistent.example.com"
    assert result['status_code'] == 0
    assert '[Error: Connection failed - could not reach the server]' in result['content']
    assert result['error'] == 'Connection error'


def test_fetch_url_invalid_url():
    """Test handling of invalid URLs."""
    with pytest.raises(ValueError, match="Invalid URL"):
        fetch_url("not-a-url")
    
    with pytest.raises(ValueError, match="Invalid URL"):
        fetch_url("ftp://example.com")
    
    with pytest.raises(ValueError, match="Invalid URL"):
        fetch_url("")


@patch('requests.get')
def test_fetch_url_unsupported_content_type(mock_get):
    """Test handling of non-HTML content types."""
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.headers = {'content-type': 'application/pdf'}
    mock_response.content = b"PDF content"
    mock_get.return_value = mock_response
    
    result = fetch_url("https://example.com/document.pdf")
    
    assert result['url'] == "https://example.com/document.pdf"
    assert result['status_code'] == 200
    assert 'Content type application/pdf not supported' in result['content']
    assert result['error'] == 'Unsupported content type: application/pdf'


@patch('requests.get')
def test_fetch_url_max_chars_parameter(mock_get):
    """Test that max_chars parameter is respected."""
    mock_response = Mock()
    mock_response.text = f"<html><body><p>{'A' * 200}</p></body></html>"
    mock_response.status_code = 200
    mock_response.headers = {'content-type': 'text/html'}
    mock_get.return_value = mock_response
    
    result = fetch_url("https://example.com", max_chars=50)
    
    assert len(result['content']) <= 53  # 50 + "..."
    assert result['content'].endswith('...')


@patch('requests.get')
def test_fetch_url_title_extraction(mock_get):
    """Test title extraction from HTML."""
    mock_response = Mock()
    mock_response.text = """
    <html>
        <head>
            <title>  Whitespace Title  </title>
        </head>
        <body><p>Content</p></body>
    </html>
    """
    mock_response.status_code = 200
    mock_response.headers = {'content-type': 'text/html'}
    mock_get.return_value = mock_response
    
    result = fetch_url("https://example.com")
    
    assert result['title'] == "Whitespace Title"  # Should be stripped


@patch('requests.get')
def test_fetch_url_no_title(mock_get):
    """Test handling of HTML without title tag."""
    mock_response = Mock()
    mock_response.text = "<html><body><p>Content without title</p></body></html>"
    mock_response.status_code = 200
    mock_response.headers = {'content-type': 'text/html'}
    mock_get.return_value = mock_response
    
    result = fetch_url("https://example.com")
    
    assert result['title'] == ""
    assert "Content without title" in result['content']


def test_clean_html_to_text_markdown_conversion():
    """Test that HTML is properly converted to markdown."""
    html = """
    <html>
        <body>
            <h1>Heading 1</h1>
            <h2>Heading 2</h2>
            <p><strong>Bold text</strong> and <em>italic text</em>.</p>
            <ul>
                <li>List item 1</li>
                <li>List item 2</li>
            </ul>
        </body>
    </html>
    """
    
    result = clean_html_to_text(html)
    
    # Should convert to markdown format
    assert "# Heading 1" in result
    assert "## Heading 2" in result
    assert "**Bold text**" in result
    assert "*italic text*" in result
    assert "* List item 1" in result
    assert "* List item 2" in result


@patch('requests.get')
def test_fetch_url_redirects_followed(mock_get):
    """Test that redirects are properly followed."""
    mock_response = Mock()
    mock_response.text = "<html><body><p>Final destination</p></body></html>"
    mock_response.status_code = 200
    mock_response.headers = {'content-type': 'text/html'}
    mock_get.return_value = mock_response
    
    fetch_url("https://example.com/redirect")
    
    # Verify that allow_redirects=True was passed
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert kwargs.get('allow_redirects') is True


@patch('requests.get')
def test_fetch_url_user_agent_header(mock_get):
    """Test that proper User-Agent header is sent."""
    mock_response = Mock()
    mock_response.text = "<html><body><p>Content</p></body></html>"
    mock_response.status_code = 200
    mock_response.headers = {'content-type': 'text/html'}
    mock_get.return_value = mock_response
    
    fetch_url("https://example.com")
    
    # Verify User-Agent header was set
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    headers = kwargs.get('headers', {})
    assert 'User-Agent' in headers
    assert 'Mozilla' in headers['User-Agent']


def test_clean_html_to_text_excessive_whitespace():
    """Test removal of excessive whitespace."""
    html = """
    <html>
        <body>
            <p>Paragraph 1</p>
            
            
            
            <p>Paragraph 2</p>
        </body>
    </html>
    """
    
    result = clean_html_to_text(html)
    
    # Should not have more than double newlines
    assert "\n\n\n" not in result
    assert "Paragraph 1" in result
    assert "Paragraph 2" in result