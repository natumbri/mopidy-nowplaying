import io
import json
import logging
from pathlib import Path
import threading
from dataclasses import asdict, is_dataclass
from typing import Any, override

import tornado.escape
import tornado.web

logger = logging.getLogger(__name__)

_STATUS_LOCK = threading.Lock()
_LAST_FRAME = b""
_LAST_JPEG_FRAME = b""
_LAST_STATUS: dict[str, Any] = {
    "title": "",
    "artist": "",
    "album": "",
    "state": "stopped",
    "position_ms": 0,
    "lyrics": [],
}
_CONTROL_STATE: dict[str, bool] = {
    "framebuffer": True,
    "stream": True,
    "lyrics": True,
}


def set_control_state(name: str, enabled: bool) -> dict[str, bool]:
    if name in _CONTROL_STATE:
        _CONTROL_STATE[name] = bool(enabled)
    return dict(_CONTROL_STATE)


def get_control_state() -> dict[str, bool]:
    return dict(_CONTROL_STATE)


def update_nowplaying_state(frame: bytes | None, status: dict[str, Any] | None, mjpeg_frame: bytes | None = None) -> None:
    global _LAST_FRAME, _LAST_JPEG_FRAME, _LAST_STATUS
    with _STATUS_LOCK:
        if frame is not None:
            _LAST_FRAME = frame
        if mjpeg_frame is not None:
            _LAST_JPEG_FRAME = mjpeg_frame
        if status is not None:
            _LAST_STATUS = status


def get_nowplaying_frame() -> bytes:
    with _STATUS_LOCK:
        if not _CONTROL_STATE["stream"]:
            return b""
        return _LAST_FRAME


def get_nowplaying_mjpeg_frame() -> bytes:
    with _STATUS_LOCK:
        if not _CONTROL_STATE["stream"]:
            return b""
        return _LAST_JPEG_FRAME


def get_nowplaying_status() -> dict[str, Any]:
    with _STATUS_LOCK:
        data = _LAST_STATUS.copy()
        data["controls"] = dict(_CONTROL_STATE)
        return data


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, type(None))):
        return getattr(value, "value")
    if hasattr(value, "__dict__") and not isinstance(value, (dict, list, tuple, set)):
        return value.__dict__
    return str(value)


def _serialize_lyrics(lines: Any) -> list[dict[str, Any]]:
    if not lines:
        return []
    output: list[dict[str, Any]] = []
    for item in lines:
        if hasattr(item, "time_ms") and hasattr(item, "text"):
            output.append({"time_ms": int(getattr(item, "time_ms")), "text": getattr(item, "text")})
        elif isinstance(item, dict):
            output.append({
                "time_ms": int(item.get("time_ms") or 0),
                "text": item.get("text") or "",
            })
        else:
            output.append({"text": str(item)})
    return output


def status_snapshot(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {
            "title": "",
            "artist": "",
            "album": "",
            "state": "stopped",
            "position_ms": 0,
            "lyrics": [],
        }

    track = payload.get("track")
    raw_artists = []
    if isinstance(track, dict):
        raw_artists = track.get("artists") or []
        title = track.get("name") or payload.get("title") or ""
        album = (track.get("album") or {}).get("name") or payload.get("album") or ""
    else:
        title = getattr(track, "name", "") or payload.get("title") or ""
        raw_artists = getattr(track, "artists", []) or []
        album_obj = getattr(track, "album", None)
        album = getattr(album_obj, "name", "") if album_obj else payload.get("album") or ""

    if not title and isinstance(payload.get("title"), str):
        title = payload["title"]
    if not album and isinstance(payload.get("album"), str):
        album = payload["album"]

    artist_names = []
    for artist in raw_artists:
        if isinstance(artist, dict):
            name = artist.get("name") or ""
        else:
            name = getattr(artist, "name", "") or ""
        if name:
            artist_names.append(name)
    artist = ", ".join(sorted(set(artist_names))) or payload.get("artist") or ""

    state = payload.get("state")
    if hasattr(state, "value"):
        state = getattr(state, "value")
    state = str(state) if state is not None else "stopped"

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "state": state,
        "position_ms": int(payload.get("position_ms") or 0),
        "lyrics": _serialize_lyrics(payload.get("lyrics") or []),
    }


class NowPlayingControlPageHandler(tornado.web.RequestHandler):
    def initialize(self, config: Any | None = None, core: Any | None = None) -> None:
        self.config = config
        self.core = core

    def get(self) -> None:
        controls = get_control_state()
        self.render(
                "index.html",
                framebuffer=controls['framebuffer'],
                stream=controls['stream'],
                lyrics=controls['lyrics'],
            )

    @override
    def get_template_path(self) -> str:
        return str(Path(__file__).parent / "www")

class NowPlayingToggleHandler(tornado.web.RequestHandler):
    def initialize(self, config: Any | None = None, core: Any | None = None) -> None:
        self.config = config
        self.core = core

    def get(self) -> None:
        name = self.get_argument("name", default="")
        value = self.get_argument("value", default="")
        if name in {"framebuffer", "stream", "lyrics"} and value in {"on", "off"}:
            set_control_state(name, value == "on")
        self.redirect("/nowplaying/")


class NowPlayingStatusHandler(tornado.web.RequestHandler):
    def initialize(self, config: Any | None = None, core: Any | None = None) -> None:
        self.config = config
        self.core = core

    def get(self) -> None:
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps(get_nowplaying_status(), default=_json_default))


class NowPlayingImageHandler(tornado.web.RequestHandler):
    def initialize(self, config: Any | None = None, core: Any | None = None) -> None:
        self.config = config
        self.core = core

    def get(self) -> None:
        if not _CONTROL_STATE["stream"]:
            self.set_status(204)
            return
        frame = get_nowplaying_frame()
        if not frame:
            self.set_status(204)
            return
        self.set_header("Content-Type", "image/png")
        self.set_header("Cache-Control", "no-cache")
        self.write(frame)


class NowPlayingMJPEGHandler(tornado.web.RequestHandler):
    def initialize(self, config: Any | None = None, core: Any | None = None) -> None:
        self.config = config
        self.core = core

    async def get(self) -> None:
        self.set_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.set_header("Cache-Control", "no-cache")
        self.set_header("Connection", "close")
        try:
            while True:
                frame = get_nowplaying_mjpeg_frame()
                if frame:
                    self.write("--frame\r\n")
                    self.write("Content-Type: image/jpeg\r\n")
                    self.write(f"Content-Length: {len(frame)}\r\n\r\n")
                    self.write(frame)
                    self.write("\r\n")
                    await self.flush()
                await tornado.gen.sleep(0.25)
        except tornado.iostream.StreamClosedError:
            logger.debug("mopidy-nowplaying: MJPEG client disconnected")
            return


def make_nowplaying_app(config, core):
    return [
        (r"/", NowPlayingControlPageHandler, {"config": config, "core": core}),
        (r"/toggle", NowPlayingToggleHandler, {"config": config, "core": core}),
        (r"/status", NowPlayingStatusHandler, {"config": config, "core": core}),
        (r"/stream.png", NowPlayingImageHandler, {"config": config, "core": core}),
        (r"/stream.mjpg", NowPlayingMJPEGHandler, {"config": config, "core": core}),
    ]
