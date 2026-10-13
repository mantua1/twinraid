"""Antwortmodelle der REST-API."""

from typing import Literal

from twinraid.shared.base import StrictModel
from twinraid.shared.mode import RunMode


class DisplayNames(StrictModel):
    source: str
    target: str
    vm: str


class AppInfo(StrictModel):
    version: str
    mode: RunMode
    default_language: Literal["de", "en"]
    names: DisplayNames
