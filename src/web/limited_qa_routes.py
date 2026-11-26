"""HTTP surface of the Limited_QA integration. The browser never sees the token."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.limited_qa import client, service

router = APIRouter()
_log = logging.getLogger(__name__)

_PUBLIC = ("request_id", "status", "jira_status", "dispatch_state", "result_urls", "result_items",
           "error", "created_local", "completed_at")


def _public(rec: dict) -> dict:
    out = {k: rec.get(k) for k in _PUBLIC}
    body = rec.get("body") or {}
    out.update(source_urls=body.get("source_urls", []), target_domain=body.get("target_domain"),
               country=body.get("country"), facebook_account=body.get("facebook_account"))
    return out


@router.get("/limited-qa", response_class=HTMLResponse)
def limited_qa_page(request: Request):
    from src.web.app import templates

    return templates.TemplateResponse(request, "limited_qa.html", {"configured": client.is_configured()})


@router.get("/api/limited-qa/requests")
def list_requests():
    service.start_poller()
    return {"configured": client.is_configured(), "requests": [_public(r) for r in service.list_requests()]}


@router.post("/api/limited-qa/requests")
async def create_request(request: Request):
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"error": "Send a JSON body."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "Send a JSON body."}, status_code=400)
    try:
        rec = service.submit(data.get("source_urls"), data.get("target_domain"), data.get("country"),
                             data.get("facebook_account"))
    except client.ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except client.NotConfigured as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    service.start_poller()
    return JSONResponse(_public(rec), status_code=202)


@router.post("/api/limited-qa/requests/{request_id}/refresh")
def refresh_request(request_id: str):
    try:
        rid = client.normalize_request_id(request_id)
    except client.ValidationError:
        return JSONResponse({"error": "Invalid request id."}, status_code=400)
    rec = service.refresh(rid)
    if rec is None:
        return JSONResponse({"error": "Request not found."}, status_code=404)
    return _public(rec)
