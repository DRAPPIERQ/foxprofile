"""Download the videos recorded from a profile's tab (see page/record/*)."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ...services.browser.recording import is_recording_file, recordings_dir
from ...utils.validation import validate_profile_name

router = APIRouter(prefix="/recordings", tags=["recordings"])


@router.get("/{name}/{file}", responses={200: {"content": {"video/webm": {}}}})
def download_recording(name: str, file: str) -> FileResponse:
    """A saved recording as WebM. Recordings outlive their profile, so the name is
    only checked, not looked up."""
    ok, _ = validate_profile_name(name, existing=True)
    if not ok or not is_recording_file(file):
        raise HTTPException(status_code=404, detail="Recording not found")
    directory = os.path.realpath(recordings_dir(name))
    path = os.path.realpath(os.path.join(directory, file))
    if os.path.dirname(path) != directory or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Recording not found")
    return FileResponse(path, media_type="video/webm", filename=file)
