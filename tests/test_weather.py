"""Tests for the weather executor."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from executors.weather.executor import (
    WMO_CODES,
    _geocode,
    _weather_description,
    fetch_weather,
)


def _mock_geocode_response() -> dict:
    return {
        "results": [
            {
                "name": "Denver",
                "admin1": "Colorado",
                "latitude": 39.7392,
                "longitude": -104.9847,
            }
        ]
    }


def _mock_forecast_response() -> dict:
    return {
        "current": {
            "temperature_2m": 55.4,
            "relative_humidity_2m": 30,
            "apparent_temperature": 52.1,
            "weather_code": 1,
            "wind_speed_10m": 8.5,
        },
        "daily": {
            "time": ["2026-02-16", "2026-02-17", "2026-02-18"],
            "temperature_2m_max": [65.5, 58.2, 50.0],
            "temperature_2m_min": [38.5, 35.0, 28.0],
            "weather_code": [1, 3, 61],
        },
    }


class TestWeatherDescription:
    def test_known_code(self) -> None:
        assert _weather_description(0) == "Clear sky"
        assert _weather_description(61) == "Slight rain"
        assert _weather_description(95) == "Thunderstorm"

    def test_unknown_code(self) -> None:
        result = _weather_description(999)
        assert "Unknown" in result


class TestGeocode:
    @patch("executors.weather.executor.httpx.get")
    def test_geocode_success(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_geocode_response()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _geocode("Denver")
        assert result["name"] == "Denver"
        assert result["latitude"] == 39.7392

    @patch("executors.weather.executor.httpx.get")
    def test_geocode_not_found(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": None}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with pytest.raises(ValueError, match="Location not found"):
            _geocode("Nonexistentville")

    @patch("executors.weather.executor.httpx.get")
    def test_geocode_empty_results(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with pytest.raises(ValueError, match="Location not found"):
            _geocode("Nowhere")


class TestFetchWeather:
    @patch("executors.weather.executor.httpx.get")
    def test_fetch_weather_success(self, mock_get: MagicMock) -> None:
        geo_resp = MagicMock()
        geo_resp.json.return_value = _mock_geocode_response()
        geo_resp.raise_for_status = MagicMock()

        forecast_resp = MagicMock()
        forecast_resp.json.return_value = _mock_forecast_response()
        forecast_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [geo_resp, forecast_resp]

        result = fetch_weather("Denver")

        assert result["location"] == "Denver, Colorado"
        assert result["temp_f"] == "55.4"
        assert result["feels_like_f"] == "52.1"
        assert result["condition"] == "Mainly clear"
        assert result["humidity"] == "30"
        assert result["wind_mph"] == "8.5"
        assert len(result["forecast"]) == 3
        assert result["forecast"][0]["high_f"] == "65.5"
        assert result["forecast"][0]["low_f"] == "38.5"
        assert result["forecast"][0]["condition"] == "Mainly clear"
        assert result["forecast"][2]["condition"] == "Slight rain"

    @patch("executors.weather.executor.httpx.get")
    def test_fetch_weather_no_admin(self, mock_get: MagicMock) -> None:
        geo_data = _mock_geocode_response()
        del geo_data["results"][0]["admin1"]
        geo_resp = MagicMock()
        geo_resp.json.return_value = geo_data
        geo_resp.raise_for_status = MagicMock()

        forecast_resp = MagicMock()
        forecast_resp.json.return_value = _mock_forecast_response()
        forecast_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [geo_resp, forecast_resp]

        result = fetch_weather("Denver")
        assert result["location"] == "Denver"

    @patch("executors.weather.executor.httpx.get")
    def test_fetch_weather_empty_daily(self, mock_get: MagicMock) -> None:
        geo_resp = MagicMock()
        geo_resp.json.return_value = _mock_geocode_response()
        geo_resp.raise_for_status = MagicMock()

        forecast_data = _mock_forecast_response()
        forecast_data["daily"] = {}
        forecast_resp = MagicMock()
        forecast_resp.json.return_value = forecast_data
        forecast_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [geo_resp, forecast_resp]

        result = fetch_weather("Denver")
        assert result["forecast"] == []

    @patch("executors.weather.executor.httpx.get")
    def test_output_format_matches_contract(self, mock_get: MagicMock) -> None:
        """Ensure output keys match the expected contract for the LLM prompt."""
        geo_resp = MagicMock()
        geo_resp.json.return_value = _mock_geocode_response()
        geo_resp.raise_for_status = MagicMock()

        forecast_resp = MagicMock()
        forecast_resp.json.return_value = _mock_forecast_response()
        forecast_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [geo_resp, forecast_resp]

        result = fetch_weather("Denver")
        expected_keys = {"location", "temp_f", "temp_c", "feels_like_f", "condition", "humidity", "wind_mph", "forecast"}
        assert set(result.keys()) == expected_keys
        for day in result["forecast"]:
            assert set(day.keys()) == {"date", "high_f", "low_f", "condition"}
