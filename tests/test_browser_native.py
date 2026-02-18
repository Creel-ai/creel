"""Tests for native browser mode (local Chrome subprocess)."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

from bridge.browser import (
    BrowserRelay,
    BrowserSession,
    _find_chrome_binary,
    _start_native_chrome,
)


class TestFindChromeBinary:
    """Test Chrome binary discovery."""

    @patch("bridge.browser.platform.system", return_value="Darwin")
    @patch("bridge.browser.os.path.isfile")
    def test_find_chrome_binary_macos(self, mock_isfile, mock_system):
        """On macOS, should find Google Chrome at the standard path."""
        mock_isfile.side_effect = lambda p: p == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

        result = _find_chrome_binary()
        assert result == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    @patch("bridge.browser.platform.system", return_value="Darwin")
    @patch("bridge.browser.os.path.isfile")
    def test_find_chromium_macos(self, mock_isfile, mock_system):
        """On macOS, should fall back to Chromium if Chrome not found."""
        def isfile_check(p):
            if p == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome":
                return False
            if p == "/Applications/Chromium.app/Contents/MacOS/Chromium":
                return True
            return False

        mock_isfile.side_effect = isfile_check

        result = _find_chrome_binary()
        assert result == "/Applications/Chromium.app/Contents/MacOS/Chromium"

    @patch("bridge.browser.platform.system", return_value="Linux")
    @patch("bridge.browser.shutil.which")
    def test_find_chrome_binary_linux(self, mock_which, mock_system):
        """On Linux, should use shutil.which to find Chrome."""
        mock_which.side_effect = lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None

        result = _find_chrome_binary()
        assert result == "/usr/bin/google-chrome"

    @patch("bridge.browser.platform.system", return_value="Linux")
    @patch("bridge.browser.shutil.which")
    def test_find_chromium_linux(self, mock_which, mock_system):
        """On Linux, should fall back to chromium-browser."""
        def which_check(name):
            if name == "chromium-browser":
                return "/usr/bin/chromium-browser"
            return None

        mock_which.side_effect = which_check

        result = _find_chrome_binary()
        assert result == "/usr/bin/chromium-browser"

    @patch("bridge.browser.platform.system", return_value="Darwin")
    @patch("bridge.browser.os.path.isfile", return_value=False)
    def test_find_chrome_binary_not_found(self, mock_isfile, mock_system):
        """Should raise FileNotFoundError when no Chrome is found."""
        with pytest.raises(FileNotFoundError, match="No Chrome/Chromium"):
            _find_chrome_binary()

    @patch("bridge.browser.platform.system", return_value="Linux")
    @patch("bridge.browser.shutil.which", return_value=None)
    def test_find_chrome_binary_not_found_linux(self, mock_which, mock_system):
        """Should raise FileNotFoundError on Linux when Chrome not found."""
        with pytest.raises(FileNotFoundError, match="No Chrome/Chromium"):
            _find_chrome_binary()


class TestStartNativeChrome:
    """Test native Chrome subprocess launch."""

    @patch("bridge.browser.subprocess.Popen")
    @patch("bridge.browser.tempfile.mkdtemp", return_value="/tmp/creel-chrome-test123")
    def test_create_native_lifecycle(self, mock_mkdtemp, mock_popen):
        """Verify Chrome is launched with correct args and CDP port is parsed."""
        process_mock = MagicMock()
        process_mock.poll.return_value = None
        process_mock.pid = 12345

        # Simulate Chrome writing DevTools listening line to stderr
        process_mock.stderr.readline.return_value = (
            b"DevTools listening on ws://127.0.0.1:41234/devtools/browser/abc\n"
        )
        mock_popen.return_value = process_mock

        process, port, temp_dir = _start_native_chrome("/usr/bin/google-chrome", headless=True)

        assert port == 41234
        assert temp_dir == "/tmp/creel-chrome-test123"
        assert process is process_mock

        # Check Chrome was launched with correct flags
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "/usr/bin/google-chrome"
        assert "--remote-debugging-port=0" in cmd
        assert "--user-data-dir=/tmp/creel-chrome-test123" in cmd
        assert "--headless=new" in cmd
        assert "--no-first-run" in cmd

    @patch("bridge.browser.subprocess.Popen")
    @patch("bridge.browser.tempfile.mkdtemp", return_value="/tmp/creel-chrome-test456")
    def test_native_uses_temp_profile(self, mock_mkdtemp, mock_popen):
        """Verify --user-data-dir points to temp dir."""
        process_mock = MagicMock()
        process_mock.poll.return_value = None
        process_mock.stderr.readline.return_value = (
            b"DevTools listening on ws://127.0.0.1:55555/devtools/browser/xyz\n"
        )
        mock_popen.return_value = process_mock

        _, _, temp_dir = _start_native_chrome("/usr/bin/chrome")

        assert temp_dir == "/tmp/creel-chrome-test456"
        cmd = mock_popen.call_args[0][0]
        assert f"--user-data-dir={temp_dir}" in cmd

    @patch("bridge.browser.shutil.rmtree")
    @patch("bridge.browser.subprocess.Popen")
    @patch("bridge.browser.tempfile.mkdtemp", return_value="/tmp/creel-chrome-fail")
    def test_native_chrome_port_detection_failure(self, mock_mkdtemp, mock_popen, mock_rmtree):
        """Should raise RuntimeError if port can't be detected."""
        process_mock = MagicMock()
        # Simulate Chrome exiting immediately
        process_mock.poll.return_value = 1
        process_mock.stderr.readline.return_value = b""
        mock_popen.return_value = process_mock

        with pytest.raises(RuntimeError, match="Failed to detect"):
            _start_native_chrome("/usr/bin/chrome")

        process_mock.terminate.assert_called_once()
        mock_rmtree.assert_called_once()

    @patch("bridge.browser.subprocess.Popen")
    @patch("bridge.browser.tempfile.mkdtemp", return_value="/tmp/creel-chrome-nohead")
    def test_native_non_headless(self, mock_mkdtemp, mock_popen):
        """When headless=False, --headless=new should not be in args."""
        process_mock = MagicMock()
        process_mock.poll.return_value = None
        process_mock.stderr.readline.return_value = (
            b"DevTools listening on ws://127.0.0.1:9876/devtools/browser/abc\n"
        )
        mock_popen.return_value = process_mock

        _start_native_chrome("/usr/bin/chrome", headless=False)

        cmd = mock_popen.call_args[0][0]
        assert "--headless=new" not in cmd


class TestCloseNativeSession:
    """Test native session cleanup."""

    @pytest.mark.asyncio
    async def test_close_native_terminates_process(self):
        """Verify process.terminate() is called when closing native session."""
        relay = BrowserRelay()

        browser_mock = AsyncMock()
        process_mock = MagicMock()
        session = BrowserSession(
            session_id="native-1",
            mode="native",
            browser=browser_mock,
            process=process_mock,
            temp_profile_dir="/tmp/creel-chrome-test",
        )
        relay._sessions["native-1"] = session

        with patch("bridge.browser.shutil.rmtree") as mock_rmtree:
            await relay.close_session("native-1")

        assert "native-1" not in relay._sessions
        browser_mock.close.assert_called_once()
        process_mock.terminate.assert_called_once()
        mock_rmtree.assert_called_once_with("/tmp/creel-chrome-test", ignore_errors=True)
