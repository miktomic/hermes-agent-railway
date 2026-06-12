from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

DEFAULT_TOLERANCE_SECONDS = 300
DEFAULT_TRIAGE_JOB_ID = "3221f70c4b0a"


def _secret() -> str:
    return os.environ.get("FEEDBACK_HUB_HERMES_WEBHOOK_SECRET", "").strip()


def _triage_job_id() -> str:
    return os.environ.get("FEEDBACK_HUB_TRIAGE_JOB_ID", DEFAULT_TRIAGE_JOB_ID).strip() or DEFAULT_TRIAGE_JOB_ID


def _verify_signature(body: bytes, timestamp: str, signature_header: str, secret: str) -> bool:
    if not (body and timestamp and signature_header and secret):
        return False
    if not signature_header.startswith("sha256="):
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > DEFAULT_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, f"sha256={expected}")


async def feedback_hub_issue_created(request: Request):
    secret = _secret()
    if not secret:
        return JSONResponse({"status": "error", "error": "bridge_secret_not_configured"}, status_code=500)

    body = await request.body()
    timestamp = request.headers.get("x-feedback-hub-timestamp", "")
    signature = request.headers.get("x-feedback-hub-signature", "")
    if not _verify_signature(body, timestamp, signature, secret):
        return JSONResponse({"status": "error", "error": "invalid_signature"}, status_code=401)

    try:
        payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid_json"}, status_code=400)

    event_type = request.headers.get("x-feedback-hub-event") or str(payload.get("event_type") or "")
    feedback = payload.get("feedback") or {}
    source_app = str(feedback.get("source_app") or "")
    feedback_id = feedback.get("id")

    if event_type != "issue.created":
        return JSONResponse({"status": "ignored", "reason": "event_type", "event_type": event_type}, status_code=200)
    if source_app != "career-mentor":
        return JSONResponse({"status": "ignored", "reason": "source_app", "source_app": source_app}, status_code=200)

    try:
        from hermes_cli.web_server import _call_cron_for_profile, _find_cron_job_profile

        job_id = _triage_job_id()
        profile = _find_cron_job_profile(job_id)
        if not profile:
            return JSONResponse({"status": "error", "error": "cron_job_not_found", "job_id": job_id}, status_code=404)
        job = _call_cron_for_profile(profile, "trigger_job", job_id)
        return JSONResponse({
            "status": "queued",
            "job_id": job_id,
            "profile": profile,
            "feedback_id": feedback_id,
            "event_type": event_type,
            "source_app": source_app,
            "job": job,
        }, status_code=202)
    except Exception as exc:
        return JSONResponse({"status": "error", "error": "cron_trigger_failed", "detail": str(exc)}, status_code=500)
