"""Device pairing — the one authenticated-by-code (not bearer) endpoint.

Bootstraps trust from the Telegram root: the owner runs ``/pair`` in the control
bot, which stores a hashed one-time code; the app posts that code here to mint a
256-bit bearer token. Only the token's hash is stored; the plaintext is returned
exactly once.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from ...db import repo
from ..schemas import PairRequest, PairResponse
from ..security import hash_secret, mint_token

router = APIRouter(tags=["devices"])


@router.post("/pair", response_model=PairResponse)
async def pair(body: PairRequest, request: Request) -> PairResponse:
    rt = request.app.state.rt
    code_hash = hash_secret(rt.settings.api_token_pepper, body.code.strip())
    # Single-use: consume atomically. False = unknown / already used / expired.
    if not repo.api_pair_code_consume(rt.db, code_hash):
        rt.audit.note("api_pair_rejected")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid, used, or expired pair code",
        )
    token = mint_token()
    token_hash = hash_secret(rt.settings.api_token_pepper, token)
    device_id = repo.api_device_create(
        rt.db,
        name=body.device_name,
        token_hash=token_hash,
        device_pubkey=body.device_pubkey,
        push_endpoint=body.push_endpoint,
    )
    rt.audit.note("api_device_paired", device_id=device_id, name=body.device_name)
    return PairResponse(token=token, device_id=device_id)
