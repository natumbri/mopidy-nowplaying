import io
import json
import logging
import threading
from dataclasses import asdict, is_dataclass
from typing import Any

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
        html = f"""
        <html><head><meta charset="utf-8"><title>Mopidy Nowplaying</title>
        <link rel="stylesheet" href="/mopidy/mopidy.css" type="text/css" media="screen, print">
        <style>
            .control-row {{ display: flex; align-items: center; gap: 0.75rem; margin: 0.9rem 0; flex-wrap: wrap; }}
            .control-label {{ min-width: 120px; font-weight: 700; color: #1f2328; }}
            .toggle-group {{ display: inline-flex; border: 1px solid #c3cbd4; border-radius: 4px; overflow: hidden; background: #f7f8fa; box-shadow: inset 0 0 0 1px rgba(27, 31, 35, 0.04); }}
            .toggle {{ display: inline-block; padding: 0.5rem 0.9rem; text-decoration: none; color: #20262d; border-right: 1px solid #c3cbd4; background: #edf1f5; line-height: 1.2; font-weight: 700; }}
            .toggle:last-child {{ border-right: none; }}
            .toggle:hover {{ opacity: 1; text-decoration: none; }}
            .toggle.on {{ background: #dff4e5; color: #1b4833; }}
            .toggle.off {{ background: #f8dfe0; color: #5e2325; }}
            .link-group {{ display: flex; flex-wrap: wrap; gap: 0.5rem; }}
        </style></head><body>
        <div class="box focus">
          <h1>Mopidy Nowplaying</h1>
          <p>
            This page allows you to control the Mopidy Nowplaying extension and view its status.
          </p>
        </div>
        <div class="box">
            <div class="control-row">
                <span class="control-label">Framebuffer</span>
                <div class="toggle-group">
                    <a class="toggle {'on' if controls['framebuffer'] else 'off'}" href="/nowplaying/toggle?name=framebuffer&value=on">On</a>
                    <a class="toggle {'off' if controls['framebuffer'] else 'on'}" href="/nowplaying/toggle?name=framebuffer&value=off">Off</a>
                </div>
            </div>
            <div class="control-row">
                <span class="control-label">HTTP stream</span>
                <div class="toggle-group">
                    <a class="toggle {'on' if controls['stream'] else 'off'}" href="/nowplaying/toggle?name=stream&value=on">On</a>
                    <a class="toggle {'off' if controls['stream'] else 'on'}" href="/nowplaying/toggle?name=stream&value=off">Off</a>
                </div>
            </div>
            <div class="control-row">
                <span class="control-label">Lyrics</span>
                <div class="toggle-group">
                    <a class="toggle {'on' if controls['lyrics'] else 'off'}" href="/nowplaying/toggle?name=lyrics&value=on">On</a>
                    <a class="toggle {'off' if controls['lyrics'] else 'on'}" href="/nowplaying/toggle?name=lyrics&value=off">Off</a>
                </div>
            </div>
        </div>
        <div class="box">
            <div class="link-group">
                <a class="toggle" href="/nowplaying/status">JSON status</a>
                <a class="toggle" href="/nowplaying/stream.png">PNG stream</a>
                <a class="toggle" href="/nowplaying/stream.mjpg">MJPEG stream</a>
            </div>
        </div>
        </body></html>
        """
        self.write(html)


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
