# mopidy-nowplaying

Mopidy extension that renders the current track, album art, and synced lyrics onto a display and exposes them over HTTP for browser access.

This project is primarily a Mopidy service. It can render to a framebuffer and it also serves a small web UI and live image/stream endpoints for local access.

## Features

- Render album art and lyrics to a framebuffer or local display surface
- Serve live status JSON
- Serve live image streams:
  - `/nowplaying/stream.png`
  - `/nowplaying/stream.mjpg`
- Expose a control page at `/nowplaying/`
- Toggle framebuffer output, HTTP stream output, and lyrics on/off

## Installation

Install from PyPI:

```sh
python3 -m pip install mopidy-nowplaying
```

For local development:

```sh
git clone https://github.com/natumbri/mopidy-nowplaying.git
cd mopidy-nowplaying
python3 -m pip install -e .
```

## Configuration

Add the extension to your Mopidy config file:

```ini
[nowplaying]
enabled = true
lyrics_enabled = true
lyrics_offset_ms = 0
lyrics_font =
```

Available settings:

- `enabled`: enable the extension
- `lyrics_enabled`: show synced lyrics when available
- `lyrics_offset_ms`: offset lyric timing in milliseconds
- `lyrics_font`: optional font path for lyrics rendering

If you are using the framebuffer output, the user running Mopidy must have permission to access the display device or framebuffer. On Linux this often means adding the Mopidy user to the `video` group.

## HTTP endpoints

When the Mopidy HTTP extension is enabled, the Now Playing app is exposed under the Mopidy web root:

- `http://<host>:6680/nowplaying/` - control page
- `http://<host>:6680/nowplaying/status` - JSON status
- `http://<host>:6680/nowplaying/stream.png` - PNG snapshot
- `http://<host>:6680/nowplaying/stream.mjpg` - MJPEG stream
- `http://<host>:6680/nowplaying/toggle?name=framebuffer&value=off` - toggle control

## Development

### Set up a dev environment

```sh
cd mopidy-nowplaying
python3 -m pip install -U pip
python3 -m pip install -e .[dev]
```

### Run tests

```sh
pytest
```

### Run linting and typing

```sh
ruff check .
ruff format .
pyright .
```

### Run the full tox matrix

```sh
tox
```

## Project resources

- [Source code](https://github.com/natumbri/mopidy-nowplaying)
- [Issues](https://github.com/natumbri/mopidy-nowplaying/issues)
- [Releases](https://github.com/natumbri/mopidy-nowplaying/releases)

## Credits

- Original author: [Nik Tumbri](https://github.com/natumbri)
- Current maintainer: [Nik Tumbri](https://github.com/natumbri)
- [Contributors](https://github.com/natumbri/mopidy-nowplaying/graphs/contributors)
