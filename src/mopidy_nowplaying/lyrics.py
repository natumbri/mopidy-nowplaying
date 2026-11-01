"""Synced lyrics for mopidy-nowplaying.

Three small, framebuffer-agnostic pieces:

* ``parse_lrc`` / ``Lyrics``: LRC text -> timestamped lines, and "which line is
  current at position X" lookups.
* ``LyricsProvider``: LRCLIB lookup with an on-disk cache. Blocking; call it
  from a worker thread, never from the Pykka actor thread.
* ``PositionClock``: playback position extrapolated from a monotonic clock, so
  Mopidy only has to tell us about starts, pauses, resumes and seeks.
"""

import hashlib
import json
import logging
import re
import threading
import time
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

LRCLIB_API = "https://lrclib.net/api"
REQUEST_TIMEOUT_S = 10
DURATION_TOLERANCE_S = 3
MAX_ARTIST_ATTEMPTS = 2
# "No lyrics found" is cached too, but retried after a week.
NEGATIVE_CACHE_TTL_S = 7 * 24 * 3600

_STAMP = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# Enhanced-LRC word tags such as <00:12.34>. We only do line-level sync.
_WORD_TAG = re.compile(r"<\d+:\d{1,2}(?:[.:]\d{1,3})?>")
_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Line:
    time_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class TrackInfo:
    """The bits of a Mopidy Track that a lyrics lookup needs."""

    uri: str
    title: str
    artists: tuple[str, ...]
    album: str | None
    duration_s: int | None

    @classmethod
    def from_track(cls, track) -> "TrackInfo | None":
        if track is None or not track.uri or not track.name:
            return None
        # Mopidy doesn't guarantee artist order; sort so lookups are stable.
        artists = tuple(sorted(a.name for a in (track.artists or ()) if a.name))
        if not artists:
            return None
        album = track.album.name if track.album and track.album.name else None
        length_ms = track.length
        duration_s = round(length_ms / 1000) if length_ms else None
        return cls(track.uri, track.name, artists, album, duration_s)


def parse_lrc(text: str) -> list[Line]:
    """Parse LRC text into time-sorted lines.

    Handles multiple leading timestamps per line, ``[mm:ss]``, ``[mm:ss.x]``,
    ``[mm:ss.xx]`` and ``[mm:ss.xxx]``, the ``[offset:+/-ms]`` header and
    enhanced-LRC word tags (which are stripped). Empty lines are kept: they
    mark instrumental gaps, and the display should go quiet during them.
    """
    offset_ms = 0
    if match := _OFFSET.search(text):
        offset_ms = int(match.group(1))

    lines: list[Line] = []
    for raw in text.splitlines():
        raw = raw.lstrip()
        stamps: list[int] = []
        pos = 0
        while match := _STAMP.match(raw, pos):
            minutes, seconds, fraction = match.groups()
            millis = int((fraction or "0").ljust(3, "0"))
            stamps.append((int(minutes) * 60 + int(seconds)) * 1000 + millis)
            pos = match.end()
        if not stamps:
            continue  # metadata tag ([ar:...], [ti:...]) or junk
        body = _WORD_TAG.sub("", raw[pos:]).strip()
        # LRC offset: positive means lyrics should appear sooner.
        lines.extend(Line(max(0, t - offset_ms), body) for t in stamps)

    lines.sort(key=lambda line: line.time_ms)
    return lines


class Lyrics:
    """A track's lines plus fast position lookups."""

    def __init__(self, lines: list[Line]):
        self.lines = lines
        self._times = [line.time_ms for line in lines]

    def __bool__(self) -> bool:
        return bool(self.lines)

    @staticmethod
    def _word_count(text: str) -> int:
        return len(text.split()) if text.strip() else 0

    def index_at(self, position_ms: float, lead_ms: int = 0, min_word_dwell_ms: int = 0) -> int:
        """Index of the line that should be visible at ``position_ms``.

        When ``lead_ms`` or ``min_word_dwell_ms`` are supplied, a line can become
        active slightly before its timestamp and remain visible long enough to
        keep the lyrics aligned with the music. The default behavior retains the
        original timestamp-only semantics for compatibility.
        """
        if not self.lines:
            return -1

        index = -1
        for i, line in enumerate(self.lines):
            start_ms = self._times[i] - lead_ms
            if position_ms < start_ms:
                break
            next_start_ms = self._times[i + 1] if i + 1 < len(self._times) else float("inf")
            dwell_end_ms = self._times[i] + max(min_word_dwell_ms, self._word_count(line.text) * min_word_dwell_ms)
            if position_ms < max(next_start_ms, dwell_end_ms):
                index = i
        return index

    def time_at(self, index: int) -> int | None:
        """Start time of line ``index``, or None when past the end."""
        return self._times[index] if 0 <= index < len(self._times) else None

    def text(self, index: int) -> str:
        return self.lines[index].text if 0 <= index < len(self.lines) else ""


class LyricsProvider:
    """LRCLIB lookups with a persistent cache. Thread-safe, blocking."""

    def __init__(self, session: requests.Session, cache_dir: Path):
        self._session = session
        self._dir = Path(cache_dir) / "lyrics"
        self._dir.mkdir(parents=True, exist_ok=True)

    def get(self, info: TrackInfo) -> list[Line]:
        """Synced lines for the track, or [] if there are none."""
        cached = self._read_cache(info.uri)
        if cached is not None:
            return cached
        try:
            lines = self._fetch(info)
        except requests.RequestException as exc:
            # Network trouble is transient: don't poison the cache with it.
            logger.warning("mopidy-nowplaying: LRCLIB lookup failed for %r: %s", info.title, exc)
            return []
        self._write_cache(info.uri, lines)
        return lines

    # -- LRCLIB ---------------------------------------------------------

    def _fetch(self, info: TrackInfo) -> list[Line]:
        # /api/get is exact and fast; /api/search is slower and fuzzier, so it
        # is only the fallback (this is LRCLIB's own recommendation).
        for artist in info.artists[:MAX_ARTIST_ATTEMPTS]:
            lines = _synced(self._get_exact(info, artist))
            if lines:
                return lines
        return _synced(self._search(info))

    def _get_exact(self, info: TrackInfo, artist: str) -> dict | None:
        params: dict[str, str | int] = {"track_name": info.title, "artist_name": artist}
        if info.album:
            params["album_name"] = info.album
        if info.duration_s:
            params["duration"] = info.duration_s
        response = self._request_json(f"{LRCLIB_API}/get", params)
        if response is None:
            return None
        return response

    def _search(self, info: TrackInfo) -> dict | None:
        params = {"track_name": info.title, "artist_name": info.artists[0]}
        response = self._request_json(f"{LRCLIB_API}/search", params)
        if response is None:
            return None
        records = [r for r in response if r.get("syncedLyrics")]
        if info.duration_s:
            # A wrong-length version would drift out of sync, so insist.
            records = [
                r for r in records if abs((r.get("duration") or 0) - info.duration_s) <= DURATION_TOLERANCE_S
            ]
            records.sort(key=lambda r: abs(r["duration"] - info.duration_s))
        return records[0] if records else None

    def _request_json(self, url: str, params: dict[str, str | int]) -> dict | list | None:
        for attempt in range(3):
            try:
                response = self._session.get(url, params=params, timeout=REQUEST_TIMEOUT_S)
                if response.status_code == 404:
                    return None
                if response.status_code == 503 and attempt < 2:
                    logger.warning(
                        "mopidy-nowplaying: LRCLIB 503 for %s, retrying (%s/3)",
                        url,
                        attempt + 1,
                    )
                    time.sleep(0.1 * (attempt + 1))
                    continue
                response.raise_for_status()
                return response.json()
            except requests.HTTPError as exc:
                if getattr(exc.response, "status_code", None) == 503 and attempt < 2:
                    logger.warning(
                        "mopidy-nowplaying: LRCLIB 503 for %s, retrying (%s/3)",
                        url,
                        attempt + 1,
                    )
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise
        return None

    # -- cache ----------------------------------------------------------

    def _path(self, uri: str) -> Path:
        return self._dir / f"{hashlib.sha256(uri.encode()).hexdigest()[:32]}.json"

    def _read_cache(self, uri: str) -> list[Line] | None:
        try:
            data = json.loads(self._path(uri).read_text(encoding="utf-8"))
            if data.get("lines"):
                return [Line(int(ms), str(text)) for ms, text in data["lines"]]
            if time.time() - float(data.get("ts", 0)) < NEGATIVE_CACHE_TTL_S:
                return []
        except (OSError, ValueError, TypeError):
            pass
        return None

    def _write_cache(self, uri: str, lines: list[Line]) -> None:
        path = self._path(uri)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        payload = {"ts": time.time(), "lines": [[line.time_ms, line.text] for line in lines]}
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning("mopidy-nowplaying: could not write lyrics cache: %s", exc)


def _synced(record: dict | None) -> list[Line]:
    if not record or record.get("instrumental"):
        return []
    return parse_lrc(record.get("syncedLyrics") or "")


class PositionClock:
    """Playback position, extrapolated between Mopidy events.

    Mopidy's position query is coarse and slow, so we ask for nothing: the
    events already carry the position (pause, resume, seek) and a track start
    is position zero.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._base_ms = 0.0
        self._base_t = time.monotonic()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def now_ms(self) -> float:
        with self._lock:
            return self._now_locked()

    def set_position(self, position_ms: float) -> None:
        with self._lock:
            self._base_ms = float(position_ms)
            self._base_t = time.monotonic()

    def set_running(self, running: bool) -> None:
        with self._lock:
            self._base_ms = self._now_locked()
            self._base_t = time.monotonic()
            self._running = running

    def _now_locked(self) -> float:
        if not self._running:
            return self._base_ms
        return self._base_ms + (time.monotonic() - self._base_t) * 1000.0
