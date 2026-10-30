"""Pure-function tests for the Telegram bot's IoT device rendering.

No network and no session: the buoy payload shape is the hardware's own (see
app/schemas/iot.py), so these pin the two behaviours that matter when firmware
changes underneath us -- every reported field reaches the card, and a field the
firmware omitted is never rendered as a measurement.
"""

from __future__ import annotations

import os
from typing import Any

# app.telegram.bot calls load_dotenv() at import time -- it is a standalone script,
# and that is how it finds the repo-root .env. Importing it here would leak the real
# .env into os.environ for the whole pytest session, and settings read from the
# environment: GEE_ENABLED=true in particular makes OPTIONAL_CHECKS overwrite the
# monkeypatched probe in tests/test_health.py. So snapshot the environment around the
# import and put it back.
_env_before = dict(os.environ)
from app.telegram.bot import (  # noqa: E402
    _signal_bar,
    _tds_reading,
    _turbidity_reading,
    format_device_analysis,
    format_device_card,
)
from app.telegram.tools import _flatten_telemetry  # noqa: E402

os.environ.clear()
os.environ.update(_env_before)

FULL_PAYLOAD: dict[str, Any] = {
    "device": "jalnetra-buoy-01",
    "timestamp_ms": 1759106400000,
    "temperature": {"value": 27.84, "unit": "C"},
    "tds": {"value": 412.5, "unit": "ppm", "adc": 1863, "voltage": 1.502},
    "turbidity": {"value": 18.7, "unit": "NTU", "adc": 2471, "voltage": 1.994, "status": "ok"},
    "wifi": {"rssi": -63, "ip": "192.168.244.250"},
}


def test_flatten_exposes_every_reported_field() -> None:
    flat = _flatten_telemetry(FULL_PAYLOAD)
    assert flat["online"] is True
    assert flat["device"] == "jalnetra-buoy-01"
    assert flat["temperature"] == 27.84
    assert (flat["tds"], flat["tds_adc"], flat["tds_voltage"]) == (412.5, 1863, 1.502)
    assert (flat["turbidity"], flat["turbidity_adc"]) == (18.7, 2471)
    assert flat["turbidity_voltage"] == 1.994
    assert flat["turbidity_status"] == "ok"
    assert flat["wifi_rssi"] == -63
    assert flat["reported_at"] is not None


def test_flatten_survives_a_nonsense_device_clock() -> None:
    """Some firmware sends uptime, not epoch ms -- that must not raise."""
    flat = _flatten_telemetry({**FULL_PAYLOAD, "timestamp_ms": 10**15})
    assert flat["reported_at"] is None
    assert flat["timestamp_ms"] == 10**15


def test_flatten_tolerates_an_empty_payload() -> None:
    flat = _flatten_telemetry({})
    assert flat["online"] is True  # the device answered; it just said little
    assert flat["temperature"] is None
    assert flat["device"] == "unknown"


def test_card_carries_every_measurement_and_diagnostic() -> None:
    card = format_device_card(_flatten_telemetry(FULL_PAYLOAD))
    for expected in ("27.84", "412.5", "18.7", "1863", "1.994", "2471", "-63"):
        assert expected in card, f"{expected} missing from the card"
    assert "jalnetra-buoy-01" in card
    assert "192.168.244.250" in card


def test_card_never_invents_a_reading_the_firmware_omitted() -> None:
    """A missing probe must read as unreported, not as 0."""
    card = format_device_card(_flatten_telemetry({"temperature": {"value": 31.2}}))
    assert "31.2" in card
    assert "—" in card  # TDS/turbidity unreported
    assert "0 ppm" not in card
    assert "0 NTU" not in card


def test_offline_card_explains_itself_without_numbers() -> None:
    card = format_device_card({"online": False, "error": "buoy unreachable (10.0.0.5)"})
    assert "OFFLINE" in card
    assert "10.0.0.5" in card
    # It must not imply a reading was taken.
    assert "Temperature" not in card


def test_card_keeps_the_single_point_caveat() -> None:
    """The product boundary: indicative, never a lab result."""
    card = format_device_card(_flatten_telemetry(FULL_PAYLOAD))
    assert "not a lab assay" in card


def test_analysis_render_includes_actions_and_outlook() -> None:
    rendered = format_device_analysis(
        {
            "online": True,
            "water_body": "Khadakwasla Dam",
            "analysis": {
                "condition_summary": "Moderate quality.",
                "parameters": {
                    "temperature_analysis": "Stable.",
                    "tds_analysis": "Acceptable.",
                    "turbidity_analysis": "Slightly raised.",
                },
                "suggestions": ["Monitor TDS", "Add filtration"],
                "future_prediction": "Turbidity may spike with runoff.",
            },
        }
    )
    assert "Khadakwasla Dam" in rendered
    assert "Moderate quality." in rendered
    assert "1. Monitor TDS" in rendered
    assert "outlook" in rendered
    assert "not a scored JalNetra finding" in rendered


def test_analysis_render_tolerates_a_sparse_ai_reply() -> None:
    rendered = format_device_analysis({"online": True, "analysis": {}})
    assert "No summary returned." in rendered


def test_a_percentage_turbidity_is_not_scored_on_ntu_guidelines() -> None:
    """The pilot ESP32 reports turbidity in %, with no calibration to reach NTU.

    Scoring 29.9 % against the <1 / <=5 NTU drinking-water bands would read as a
    guideline breach the device never measured, so no band is offered at all.
    """
    assert _turbidity_reading(29.9, "%") == ""
    assert _turbidity_reading(29.9, "NTU") == "slightly turbid"

    card = format_device_card(
        {
            "online": True,
            "device": "JAL NETRA ESP32",
            "turbidity": 29.9,
            "turbidity_unit": "%",
            "tds": 69.0,
            "tds_unit": "ppm",
            "device_status": "NORMAL",
            "source": "html",
        }
    )
    assert "29.9 %" in card
    assert "NORMAL" in card
    for band in ("very clear", "clear", "slightly turbid", "turbid", "very turbid"):
        assert band not in card


def test_reading_bands_are_ordered() -> None:
    assert _turbidity_reading(0.4) == "very clear"
    assert _turbidity_reading(4.0) == "clear"
    assert _turbidity_reading(18.7) == "slightly turbid"
    assert _turbidity_reading(240.0) == "very turbid"
    assert _tds_reading(412.5) == "good — typical freshwater"
    assert _tds_reading(1500.0) == "very high — brackish"
    assert _signal_bar(-63) == "good"
    assert _signal_bar(-95) == "very weak"
