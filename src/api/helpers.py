"""Shared helpers for API routes (DRY)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import HTTPException

from ..core.config import DATA_DIR
from .schemas.profiles import ProfileResponse

if TYPE_CHECKING:
    from ..interfaces import IBrowserLauncher, IProfileManager
    from ..models.profile import Profile

API_PREFIX = "/api/v1"


def require_profile(name: str, pm: IProfileManager) -> Profile:
    """The profile, or 404. Use the returned object rather than indexing
    pm.profiles again: a concurrent delete can remove it in between."""
    profile = pm.profiles.get(name)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return profile


def build_profile_response(
    name: str,
    pm: IProfileManager,
    bl: IBrowserLauncher,
) -> ProfileResponse:
    """Build a ProfileResponse DTO for the given profile name."""
    return profile_response(require_profile(name, pm), bl)


def profile_response(profile: Profile, bl: IBrowserLauncher) -> ProfileResponse:
    return ProfileResponse(
        name=profile.name,
        proxy=profile.proxy,
        os_type=profile.os_type,
        timezone=profile.timezone,
        locale=profile.locale,
        engine=profile.engine,
        data_dir=os.path.join(os.getcwd(), DATA_DIR, profile.name),
        is_running=bl.is_running(profile.name),
    )


def with_download(profile_name: str, recording: dict[str, Any] | None) -> dict[str, Any] | None:
    """Add the API path a saved recording can be downloaded from."""
    if not recording or "file" not in recording:
        return recording
    path = f"{API_PREFIX}/recordings/{quote(profile_name, safe='')}/{quote(recording['file'])}"
    return {**recording, "download": path}
