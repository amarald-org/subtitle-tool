"""Minimal client for the OpenSubtitles.com REST API (v1).

Needs a free API key from https://www.opensubtitles.com/consumers.
Username/password are optional; logging in raises the daily download quota.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import requests

API = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "subtitle-tool v0.1.0"


class OpenSubtitlesError(RuntimeError):
    pass


def movie_hash(path: Path) -> str:
    """OpenSubtitles hash: file size + checksum of the first and last 64 KiB."""
    chunk = 64 * 1024
    size = path.stat().st_size
    if size < chunk * 2:
        raise ValueError("file too small to hash")
    h = size
    with path.open("rb") as f:
        for offset in (0, size - chunk):
            f.seek(offset)
            buf = f.read(chunk)
            for (v,) in struct.iter_unpack("<Q", buf):
                h = (h + v) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


@dataclass
class SubtitleHit:
    file_id: int
    release: str
    language: str
    downloads: int
    hash_match: bool
    title: str
    year: int | None

    def label(self) -> str:
        tag = " [hash match]" if self.hash_match else ""
        year = f" ({self.year})" if self.year else ""
        return f"{self.title}{year} | {self.release} | {self.downloads} dl{tag}"


def _split_year(query: str) -> tuple[str, int | None]:
    m = re.search(r"\b((?:19|20)\d{2})\s*$", query)
    if not m:
        return query.strip(), None
    return query[: m.start()].strip(), int(m.group(1))


def _norm(text: str) -> str:
    text = re.sub(r"^\d{4}\s*-\s*", "", text.lower())  # "1999 - The Matrix"
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _same_title(query: str, title: str) -> bool:
    q, t = _norm(query), _norm(title)
    return bool(q and t) and (q == t or t.startswith(q) or q.startswith(t))


class Client:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("OPENSUBTITLES_API_KEY")
        if not self.api_key:
            raise OpenSubtitlesError(
                "No OpenSubtitles API key. Get a free one at "
                "https://www.opensubtitles.com/consumers and set OPENSUBTITLES_API_KEY."
            )
        self.session = requests.Session()
        self.session.headers.update(
            {"Api-Key": self.api_key, "User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        self._maybe_login()

    def _maybe_login(self) -> None:
        user = os.environ.get("OPENSUBTITLES_USERNAME")
        pw = os.environ.get("OPENSUBTITLES_PASSWORD")
        if not (user and pw):
            return
        r = self.session.post(f"{API}/login", json={"username": user, "password": pw}, timeout=30)
        if r.ok:
            self.session.headers["Authorization"] = f"Bearer {r.json()['token']}"

    def search(
        self, query: str | None, language: str = "en", video: Path | None = None
    ) -> list[SubtitleHit]:
        params: dict[str, str] = {"languages": language, "order_by": "download_count"}
        name, year = _split_year(query or "")
        if name:
            params["query"] = name
        if year:
            params["year"] = str(year)
        if video is not None:
            try:
                params["moviehash"] = movie_hash(video)
            except (OSError, ValueError):
                pass
        r = self.session.get(f"{API}/subtitles", params=params, timeout=30)
        if not r.ok:
            raise OpenSubtitlesError(f"search failed: {r.status_code} {r.text[:200]}")
        hits = []
        for item in r.json().get("data", []):
            a = item["attributes"]
            if not a.get("files"):
                continue
            details = a.get("feature_details") or {}
            hits.append(
                SubtitleHit(
                    file_id=a["files"][0]["file_id"],
                    release=a.get("release") or "",
                    language=a.get("language") or language,
                    downloads=a.get("download_count") or 0,
                    hash_match=bool(a.get("moviehash_match")),
                    title=details.get("title") or details.get("movie_name") or "",
                    year=details.get("year"),
                )
            )
        if name:
            # Hash matches can be mis-tagged uploads of other movies, so only
            # keep results whose title actually matches what we searched for.
            matching = [h for h in hits if _same_title(name, h.title)]
            hits = matching or hits
        # A hash match is timed for this exact file, so prefer it.
        hits.sort(key=lambda h: (not h.hash_match, year is not None and h.year != year, -h.downloads))
        return hits

    def download(self, file_id: int) -> str:
        r = self.session.post(f"{API}/download", json={"file_id": file_id}, timeout=30)
        if not r.ok:
            raise OpenSubtitlesError(f"download failed: {r.status_code} {r.text[:200]}")
        link = r.json()["link"]
        content = requests.get(link, timeout=60).content
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")
