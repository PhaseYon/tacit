import json

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from . import intake
from .client import verify_signature

router = APIRouter(tags=["github"])


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(),
    x_github_delivery: str = Header(),
    x_hub_signature_256: str | None = Header(default=None),
) -> JSONResponse:
    body = await request.body()
    if not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad signature")
    if x_github_event == "ping":
        return JSONResponse({"ok": True}, status_code=status.HTTP_200_OK)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not JSON") from exc

    if not await intake.record_delivery(x_github_delivery, x_github_event, payload):
        return JSONResponse({"duplicate": True}, status_code=status.HTTP_200_OK)
    intake.enqueue_delivery(x_github_delivery)
    return JSONResponse({"queued": True}, status_code=status.HTTP_202_ACCEPTED)
