"""Alert delivery (S8, L12): webhooks and e-mail with per-recipient severity
thresholds and jurisdiction matching.

A recipient receives an alert when all of:

* it is active and the alert's severity >= its ``min_severity``;
* the alert is in its jurisdiction: the zone's representative point falls in
  ``jurisdiction_geom`` (point-in-polygon), or the water body's district is in
  ``districts``, or the recipient has neither (statewide);
* the alert has not already been sent to it at this severity or higher
  (``dispatched_severity``), unless ``reason="manual"``.

``dispatch_enabled`` is a master switch that defaults to *off* so a developer
laptop with a copied production recipients table never pages a regional
office. When off, every delivery is logged as ``skipped``.

Webhook bodies are the frozen alert JSON; ``X-JalNetra-Signature`` carries an
HMAC-SHA256 of the body under the recipient's secret when one is set.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

import httpx
from geoalchemy2.shape import to_shape
from jinja2 import Environment, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import Settings, get_settings
from app.db.models import Alert, Dispatch, Recipient, WaterBody, Zone
from app.schemas.alerts import AlertOut
from app.services.l08_anomaly.gate import SEVERITY_ORDER
from app.services.l11_alerts.payload import alert_out

log = logging.getLogger(__name__)

EMAIL_SUBJECT = "[JalNetra] {severity} anomaly - {water_body}, {zone} - {observed_on}"
EMAIL_HTML = """\
<p><strong>{{ a.water_body.name }}</strong> ({{ a.water_body.district }}) &middot;
<strong>{{ a.zone.name }}</strong> &middot; observed {{ a.observed_on }}</p>
<p>Severity <strong>{{ a.severity | upper }}</strong> &middot;
priority {{ a.priority_score | round(0) | int }}/100
&middot; confidence {{ "%.2f" | format(a.confidence) }}
{% if a.context.natural_cause_likely %}&middot;
<strong>natural cause likely (rainfall)</strong>{% endif %}</p>
<p>{{ a.explanation.summary }}</p>
<ul>
{% for c in a.explanation.contributions %}<li>{{ c.factor }}: {{ "%+.2f" | format(c.value) }}</li>
{% endfor %}</ul>
{% set rain = a.context.rainfall_72h_mm %}{% set cloud = a.context.cloud_cover_pct %}
<p>Rainfall 72 h: {{ rain if rain is not none else "n/a" }} mm
&middot; cloud over zone: {{ cloud if cloud is not none else "n/a" }} %</p>
<p><a href="{{ base }}/api/v1/alerts/{{ a.alert_id }}">Open alert</a> &middot;
<a href="{{ base }}/api/v1/alerts/{{ a.alert_id }}/brief.pdf">Investigation brief (PDF)</a></p>
<p style="color:#666;font-size:90%"><em>{{ a.disclaimer }}</em></p>
"""

_env = Environment(autoescape=select_autoescape(["html"]))
_email_template = _env.from_string(EMAIL_HTML)


class DeliveryError(RuntimeError):
    """Transient delivery failure (retryable)."""


@dataclass
class DispatchResult:
    alert_id: str
    sent: list[int] = field(default_factory=list)  # recipient ids
    skipped: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


def severity_rank(s: str) -> int:
    return SEVERITY_ORDER.index(s)


# --- matching ---------------------------------------------------------------------


def in_jurisdiction(session: Session, recipient: Recipient, wb: WaterBody, zone: Zone) -> bool:
    if recipient.districts and wb.district in recipient.districts:
        return True
    if recipient.jurisdiction_geom is not None:
        pt = to_shape(zone.geom).representative_point()
        hit = session.execute(
            select(
                func.ST_Contains(
                    Recipient.jurisdiction_geom,
                    func.ST_SetSRID(func.ST_MakePoint(pt.x, pt.y), 4326),
                )
            ).where(Recipient.id == recipient.id)
        ).scalar_one()
        return bool(hit)
    return not recipient.districts and recipient.jurisdiction_geom is None


def matching_recipients(
    session: Session, alert: Alert, wb: WaterBody, zone: Zone
) -> list[Recipient]:
    rows = session.scalars(select(Recipient).where(Recipient.active)).all()
    return [
        r
        for r in rows
        if severity_rank(alert.severity) >= severity_rank(r.min_severity)
        and in_jurisdiction(session, r, wb, zone)
    ]


# --- channels ---------------------------------------------------------------------


def sign(body: bytes, secret: str | None) -> str | None:
    if not secret:
        return None
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def send_webhook(recipient: Recipient, payload: AlertOut, *, settings: Settings) -> str:
    body = payload.model_dump_json().encode()
    headers = {"Content-Type": "application/json", "X-JalNetra-Event": "alert"}
    sig = sign(body, recipient.secret)
    if sig:
        headers["X-JalNetra-Signature"] = sig
    try:
        r = httpx.post(
            recipient.target, content=body, headers=headers, timeout=settings.dispatch_timeout_s
        )
    except httpx.HTTPError as exc:
        raise DeliveryError(f"webhook {recipient.target}: {exc}") from exc
    if r.status_code >= 500:
        raise DeliveryError(f"webhook {recipient.target}: HTTP {r.status_code}")
    if r.status_code >= 400:
        raise ValueError(f"webhook {recipient.target} rejected: HTTP {r.status_code}")
    return f"HTTP {r.status_code}"


def render_email(payload: AlertOut, *, settings: Settings) -> tuple[str, str]:
    subject = EMAIL_SUBJECT.format(
        severity=payload.severity.upper(),
        water_body=payload.water_body.name,
        zone=payload.zone.name,
        observed_on=payload.observed_on.isoformat(),
    )
    html = _email_template.render(a=payload, base=settings.public_base_url.rstrip("/"))
    return subject, html


def send_telegram(recipient: Recipient, payload: AlertOut, *, settings: Settings) -> str:
    if not settings.telegram_bot_token:
        raise ValueError("Telegram not configured (TELEGRAM_BOT_TOKEN)")
    from app.telegram.alerts import alert_data_from_payload, send_telegram_alert_sync

    alert_data = alert_data_from_payload(payload, dashboard_base_url=settings.dashboard_base_url)
    brief_url = f"{settings.public_base_url.rstrip('/')}{alert_data['brief_url']}"
    try:
        return send_telegram_alert_sync(settings.telegram_bot_token, recipient.target, alert_data, brief_url)
    except Exception as exc:
        raise DeliveryError(f"telegram {recipient.target}: {exc}") from exc


def send_email(recipient: Recipient, payload: AlertOut, *, settings: Settings) -> str:
    if not settings.smtp_host:
        raise ValueError("SMTP not configured (SMTP_HOST)")
    subject, html = render_email(payload, settings=settings)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = recipient.target
    msg.set_content(f"{payload.explanation.summary}\n\n{payload.disclaimer}")
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.dispatch_timeout_s
        ) as s:
            if settings.smtp_starttls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password or "")
            s.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise DeliveryError(f"smtp {recipient.target}: {exc}") from exc
    return "accepted"


# --- orchestration ----------------------------------------------------------------


def _log(
    session: Session, alert: Alert, r: Recipient, reason: str, status: str, detail: str | None
) -> None:
    metrics.dispatches.labels(channel=r.channel, status=status).inc()
    session.add(
        Dispatch(
            alert_id=alert.id,
            recipient_id=r.id,
            channel=r.channel,
            severity=alert.severity,
            reason=reason,
            status=status,
            detail=None if detail is None else detail[:1000],
        )
    )


def dispatch_alert(
    session: Session,
    alert: Alert,
    *,
    reason: str = "new",
    settings: Settings | None = None,
) -> DispatchResult:
    """Deliver one alert to every matching recipient. ``reason`` is ``new`` on
    creation, ``escalated`` when severity rose, ``manual`` to force a resend."""
    settings = settings or get_settings()
    result = DispatchResult(alert_id=alert.id)
    wb = session.get(WaterBody, alert.water_body_id)
    zone = session.get(Zone, alert.zone_id)
    assert wb is not None and zone is not None
    already = alert.dispatched_severity
    if (
        reason != "manual"
        and already is not None
        and severity_rank(already) >= severity_rank(alert.severity)
    ):
        log.info("dispatch: nothing new", extra={"alert_id": alert.id, "severity": alert.severity})
        return result
    recipients = matching_recipients(session, alert, wb, zone)
    if not recipients:
        return result
    payload = alert_out(alert, wb, zone)
    transient: list[str] = []
    for r in recipients:
        if not settings.dispatch_enabled:
            _log(session, alert, r, reason, "skipped", "dispatch_enabled=false")
            result.skipped.append(r.id)
            continue
        try:
            if r.channel == "webhook":
                detail = send_webhook(r, payload, settings=settings)
            elif r.channel == "telegram":
                detail = send_telegram(r, payload, settings=settings)
            else:
                detail = send_email(r, payload, settings=settings)
            _log(session, alert, r, reason, "sent", detail)
            result.sent.append(r.id)
        except DeliveryError as exc:
            _log(session, alert, r, reason, "failed", str(exc))
            result.failed.append(r.id)
            transient.append(str(exc))
        except ValueError as exc:  # misconfiguration: log, never retry
            _log(session, alert, r, reason, "failed", str(exc))
            result.failed.append(r.id)
    if result.sent or (not settings.dispatch_enabled and result.skipped):
        alert.dispatched_severity = alert.severity
    session.flush()
    log.info(
        "dispatch done",
        extra={
            "alert_id": alert.id,
            "sent": result.sent,
            "skipped": result.skipped,
            "failed": result.failed,
        },
    )
    if transient and not result.sent:
        raise DeliveryError("; ".join(transient))
    return result


def webhook_preview(alert: Alert, wb: WaterBody, zone: Zone) -> dict[str, Any]:
    """What a webhook receiver gets, for the API's dry-run endpoint."""
    body: dict[str, Any] = json.loads(alert_out(alert, wb, zone).model_dump_json())
    return body
