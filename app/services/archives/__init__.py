"""Historical archive material sources for biography videos.

Three key-less public APIs, each returning MaterialInfo-compatible records so
they can flow through the existing download/render pipeline:

  wikimedia.py            Commons  generator=search + imageinfo (license filter)
  wikipedia.py            REST page/summary (facts + portrait for the cover)
  library_of_congress.py  loc.gov/photos (supplementary US/foreign photos)

Design rules (see app/services/material.py for the provider contract):
  * Never raise: a dead API returns [] so one outage cannot kill a task.
  * License allow-list only (PD / CC0 / CC BY); CC BY-SA is excluded because
    ShareAlike would force the whole video to be relicensed.
  * Every record carries license + source page so the sidecar manifest can be
    produced for compliance review.
"""
from app.services.archives.wikimedia import (
    search_images as search_wikimedia_images,
)
from app.services.archives.wikipedia import (
    fetch_person_summary,
    resolve_english_title,
)
from app.services.archives.library_of_congress import (
    search_images as search_loc_images,
)

__all__ = [
    "search_wikimedia_images",
    "search_loc_images",
    "fetch_person_summary",
    "resolve_english_title",
]
