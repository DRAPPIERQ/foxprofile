from typing import Any

from pydantic import BaseModel

from .common import SuccessResponse


class BrowserStatusResponse(BaseModel):
    name: str
    is_running: bool


class RunningBrowsersResponse(BaseModel):
    running: list[str]
    count: int


class LaunchResponse(BaseModel):
    success: bool
    message: str


class StopResponse(SuccessResponse):
    # The recording that was still running and got saved before the stop, if any.
    recording: dict[str, Any] | None = None
