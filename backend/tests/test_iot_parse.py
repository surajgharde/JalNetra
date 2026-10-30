"""Pure-function tests for reading the IoT buoy's reply.

The pilot ESP32 serves no JSON API -- only an HTML dashboard with the readings
rendered into the page -- so the app parses that. These pin the real markup (as
served by the device at the Khadakwasla pilot) and the rule that units are never
converted or relabelled.
"""

from __future__ import annotations

from app.api.v1.iot import _parse_device_html, _payload_from_response

# Exactly what the device serves, whitespace and all.
DEVICE_PAGE = """<!DOCTYPE html>
<html><head><title>JAL NETRA</title>
<meta http-equiv="refresh" content="2">
</head>
<body>
<div class="header"><h1>JAL NETRA</h1><p>Real-Time Water Pollution Detection</p></div>
<div class="container">
<div class="card">
<div class="label">
TURBIDITY
</div>
<div class="value">
29.8
%
</div>
</div>
<div class="card">
<div class="label">
TDS
</div>
<div class="value">
68
ppm
</div>
</div>
</div>
<div class="status">
STATUS:
NORMAL
</div>
<div class="footer">JAL NETRA | ESP32 IoT Water Monitoring</div>
</body></html>"""


def test_parses_the_real_device_page() -> None:
    payload = _parse_device_html(DEVICE_PAGE)
    assert payload is not None
    assert payload["turbidity"] == {"value": 29.8, "unit": "%"}
    assert payload["tds"] == {"value": 68.0, "unit": "ppm"}
    assert payload["device_status"] == "NORMAL"
    assert payload["source"] == "html"


def test_units_are_taken_verbatim_never_converted() -> None:
    """A percentage stays a percentage: there is no calibration to reach NTU."""
    payload = _parse_device_html(DEVICE_PAGE)
    assert payload is not None
    assert payload["turbidity"]["unit"] == "%"
    assert "NTU" not in str(payload)


def test_device_status_is_not_filed_as_probe_status() -> None:
    """The page's verdict on the water is not the JSON contract's probe health."""
    payload = _parse_device_html(DEVICE_PAGE)
    assert payload is not None
    assert "status" not in payload["turbidity"]


def test_a_new_card_is_passed_through_rather_than_dropped() -> None:
    page = DEVICE_PAGE.replace(
        '<div class="status">',
        '<div class="card"><div class="label">PH</div><div class="value">7.4</div></div>'
        '<div class="card"><div class="label">DISSOLVED OXYGEN</div>'
        '<div class="value">8.1 mg/L</div></div><div class="status">',
    )
    payload = _parse_device_html(page)
    assert payload is not None
    assert payload["ph"] == {"value": 7.4}  # no unit on the page, so none invented
    assert payload["dissolved_oxygen"] == {"value": 8.1, "unit": "mg/L"}


def test_a_page_with_no_readings_is_not_a_reading() -> None:
    assert _parse_device_html("<html><body><h1>JAL NETRA</h1></body></html>") is None


def test_json_firmware_still_works_unchanged() -> None:
    """Newer firmware serving JSON must pass straight through."""
    body = '{"device": "buoy-01", "temperature": {"value": 27.8, "unit": "C"}}'
    assert _payload_from_response(body, "application/json") == {
        "device": "buoy-01",
        "temperature": {"value": 27.8, "unit": "C"},
    }


def test_json_is_detected_without_a_content_type() -> None:
    assert _payload_from_response('{"a": 1}', "") == {"a": 1}


def test_html_is_parsed_when_served_as_text_html() -> None:
    payload = _payload_from_response(DEVICE_PAGE, "text/html")
    assert payload is not None
    assert payload["tds"]["value"] == 68.0


def test_unreadable_replies_are_rejected_rather_than_guessed() -> None:
    assert _payload_from_response("", "text/html") is None
    assert _payload_from_response("not json, not html", "text/plain") is None
    assert _payload_from_response("{bad json", "application/json") is None
    # A JSON array is not a telemetry object.
    assert _payload_from_response("[1, 2, 3]", "application/json") is None
