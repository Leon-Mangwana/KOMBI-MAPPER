"""Operator session checks.

The web map's operator mode asks here whether a pasted token is right before
it unlocks the naming controls, so a typo is reported at the moment of pasting
rather than as a 401 on the first save. `require_admin` does all the work:
503 when no token is configured, 401 when the token is wrong. No database is
touched, so this is safe to call from a deployment whose database is down.
"""

from fastapi import APIRouter, Depends

from ..security import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/check", dependencies=[Depends(require_admin)])
def check() -> dict[str, str]:
    """Answer 200 if and only if the `X-Admin-Token` is valid."""
    return {"status": "ok"}
