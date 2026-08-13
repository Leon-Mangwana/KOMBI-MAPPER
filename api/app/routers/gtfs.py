"""Serving the GTFS feed.

The feed is *built* by `pipeline/gtfs.py` and only *served* here. That split is
deliberate and it is the reason this router is barely any logic at all:

- Building a feed means clustering-scale work over every route, stop and trace
  in the database — seconds to minutes, and it needs GeoPandas, Shapely and the
  rest of the pipeline's stack, which the API deliberately does not depend on
  (the two are separate installables; see ../../README.md).
- The feed changes when clustering runs, which is a batch job somebody or cron
  starts, not something a request should trigger. Consumers poll a GTFS URL on a
  schedule; rebuilding on each request would be recomputing an identical answer.

So the pipeline writes a zip and the API hands it over. `GTFS_PATH` must point
at the file `pipeline/gtfs.py` wrote.
"""

from email.utils import formatdate
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from ..config import settings

router = APIRouter(tags=["gtfs"])


@router.get(
    "/gtfs.zip",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/zip": {}}, "description": "The GTFS feed."},
        304: {"description": "The feed has not been re-exported since it was last fetched."},
        404: {"description": "No feed has been exported yet."},
    },
)
def get_gtfs(request: Request) -> Response:
    """Return the exported GTFS feed as a zip archive.

    Feeds are polled: a consumer refetches the same URL on a timer and the
    answer is usually the file it already has. So the response carries `ETag`
    and `Last-Modified` from the file itself, and a matching `If-None-Match`
    gets a 304 instead of several megabytes.
    """
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

    return _file_or_304(path, request)


def _file_or_304(path: Path, request: Request) -> Response:
    """The file, or an empty 304 when the client already has this version.

    Passing `stat_result` in makes `FileResponse` compute its validators up
    front rather than while streaming, so the `ETag` to compare against is the
    one the client would get — there is no second definition of it here to drift
    out of step with Starlette's.
    """
    stat_result = path.stat()
    response = FileResponse(
        path,
        media_type="application/zip",
        filename="gtfs.zip",
        stat_result=stat_result,
    )

    etag = response.headers["etag"]
    if etag in _if_none_match(request):
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={
                "etag": etag,
                "last-modified": formatdate(stat_result.st_mtime, usegmt=True),
            },
        )

    return response


def _if_none_match(request: Request) -> set[str]:
    """The entity tags a client says it already holds.

    The header is a comma-separated list, and a cache is entitled to weaken a
    tag it stored — `W/"abc"` and `"abc"` mean the same file here, since the
    only thing that changes it is a new export.
    """
    header = request.headers.get("if-none-match", "")
    tags = (candidate.strip() for candidate in header.split(","))
    return {tag.removeprefix("W/") for tag in tags if tag}
