import threading
from unittest.mock import Mock

from mopidy_nowplaying import Extension
from mopidy_nowplaying import frontend as frontend_lib
from mopidy_nowplaying import web


def test_get_default_config() -> None:
    ext = Extension()

    config = ext.get_default_config()

    assert "[nowplaying]" in config
    assert "enabled = true" in config


def test_get_config_schema() -> None:
    ext = Extension()

    schema = ext.get_config_schema()

    # TODO Test the content of your config schema
    # assert "username" in schema
    # assert "password" in schema




def test_make_nowplaying_app_registers_routes() -> None:
    routes = web.make_nowplaying_app(None, None)

    assert any(pattern == r"/status" for pattern, _, _ in routes)
    assert any(pattern == r"/stream.png" for pattern, _, _ in routes)


def test_status_snapshot_uses_track_data() -> None:
    snapshot = web.status_snapshot({
        "track": {"name": "Song", "artists": [{"name": "Artist"}], "album": {"name": "Album"}},
        "position_ms": 1234,
        "state": "playing",
    })

    assert snapshot["title"] == "Song"
    assert snapshot["artist"] == "Artist"
    assert snapshot["album"] == "Album"
    assert snapshot["state"] == "playing"
    assert snapshot["position_ms"] == 1234


def test_render_keeps_art_when_lyrics_disabled(monkeypatch) -> None:
    instance = frontend_lib.NowPlayingFrontend.__new__(frontend_lib.NowPlayingFrontend)
    instance.screen = Mock()
    instance._art_layer = Mock()
    instance._lock = threading.RLock()
    instance._draw_lyrics = Mock()
    instance.write_framebuffer = Mock()
    instance._web_snapshot = Mock()

    monkeypatch.setattr(frontend_lib, "get_control_state", lambda: {"framebuffer": True, "stream": True, "lyrics": False})

    instance.render()

    instance.screen.blit.assert_called_once_with(instance._art_layer, (0, 0))
    instance.screen.fill.assert_not_called()
    instance._draw_lyrics.assert_not_called()
