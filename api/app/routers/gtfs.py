"""Serving the GTFS feed.

The feed is *built* by `pipeline/gtfs.py` and only *served* here. That split is
deliberate and it is the reason this router is four lines of logic:

- Building a feed means clustering-scale work over every route, stop and trace
  in the database — seconds to minutes, and it needs GeoPandas, Shapely and the
  rest of the pipeline's stack, which the API deliberately does not depend on
  (the two are separate installables; see ../../README.md).
- The feed changes when clustering runs, which is a batch job somebody or cron
  starts, not something a request should trigger. Consumers poll a GTFS URL on a
  schedule; rebuilding on each request would be recomputing an identical answer.

So the pipeline writes a zip and the API hands it over, with `ETag` and
`Last-Modified` from the file itself so a polling consumer gets a 304 until the
next export. `GTFS_PATH` must point at the file `pipeline/gtfs.py` wrote.
"""

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from ..config import settings

router = APIRouter(tags=["gtfs"])


@router.get(
    "/gtfs.zip",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/zip": {}}, "description": "The GTFS feed."},
        404: {"description": "No feed has been exported yet."},
    },
)
def get_gtfs() -> FileResponse:
    """Return the exported GTFS feed as a zip archive."""
    path = settings.gtfs_path
    if not path.is_file():
        # 404 rather than 503: from the client's side there is simply no feed at
        # this URL yet, and the fix is an export, not a retry.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No GTFS feed has been exported yet. "
                "Run pipeline/gtfs.py, and check that GTFS_PATH points at its output."
            ),
        )

    return FileResponse(path, media_type="application/zip", filename="gtfs.zip")
