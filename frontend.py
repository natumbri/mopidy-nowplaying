import io
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pygame
import pykka
import requests
from mopidy import core, httpclient

from . import Extension
from .lyrics import Lyrics, LyricsProvider, PositionClock, TrackInfo

logger = logging.getLogger(__name__)

BLACK = (0, 0, 0)
LYRIC_CURRENT = (255, 255, 255)
LYRIC_NEIGHBOUR = (120, 120, 120)


class NowPlayingFrontend(pykka.ThreadingActor, core.CoreListener):
    """Draws album art, and synced lyrics around it, to the framebuffer.

    Threads:
      * the Pykka actor thread handles Mopidy events and must never block on
        the network, so art and lyrics are loaded on a small worker pool;
      * a ticker thread sleeps until the next lyric line is due;
      * a display thread waits for the framebuffer to exist.
    ``self._lock`` guards everything that ends up on screen. ``_generation``
    is bumped on every track change so late results from a previous track are
    discarded instead of being drawn.
    """

    def __init__(self, config, core):
        super().__init__()
        self.core = core
        self.config = config

        settings = config.get("nowplaying") or {}
        self.lyrics_enabled = settings.get("lyrics_enabled") is not False
        # Audio-output latency compensation: positive = show lyrics later.
        self.lyrics_offset_ms = settings.get("lyrics_offset_ms") or 0
        self.lyrics_font = settings.get("lyrics_font") or None

        # relies on mopidy-http to be configured and running
        http = config.get("http") or {}
        host = http.get("hostname") or "127.0.0.1"
        if host in ("::", "0.0.0.0"):  # listen-all addresses aren't connectable
            host = "127.0.0.1"
        elif ":" in host:  # bare IPv6 literal
            host = f"[{host}]"
        self.base_url = f"http://{host}:{http.get('port', 6680)}"

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": httpclient.format_user_agent()})

        # move hardcoded to config
        self.fill_percent = 0.7
        self.framebuffer_number = 0
        self.framebuffer = f"fb{self.framebuffer_number}"
        self.fb_device = f"/dev/{self.framebuffer}"

        self.surfaceSize = None
        self.scaledSize = None
        self.screen = None
        self._art_layer = None
        self._art_rect = None
        self._font_file = None
        self._fonts = {}

        self._lock = threading.RLock()
        self._generation = 0
        self._lyrics = None  # Lyrics | None
        self._line_index = None  # index drawn as current; None = not computed yet
        self._clock = PositionClock()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._provider = None
        self._executor = None

    # -- lifecycle ------------------------------------------------------

    def on_start(self):
        # 3 workers: art, lyrics, and the next track's lyrics prefetch.
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="nowplaying")
        if self.lyrics_enabled:
            try:
                self._provider = LyricsProvider(self.session, Extension.get_cache_dir(self.config))
            except Exception:
                logger.exception("mopidy-nowplaying: lyrics disabled, cache directory unavailable")
        threading.Thread(target=self._run_ticker, name="nowplaying-lyrics", daemon=True).start()
        # Spawn a background thread so Mopidy can continue loading
        self.init_thread = threading.Thread(target=self._bg_wait_for_display, daemon=True)
        self.init_thread.start()

    def on_stop(self):
        self._shutdown.set()
        self._wake.set()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._blank()

    def _bg_wait_for_display(self):
        # Indefinite loop: Check every 5 seconds if fb0 exists
        while not os.path.exists(self.fb_device):
            logger.debug(f"mopidy-nowplaying: {self.fb_device} not found. Waiting...")
            if self._shutdown.wait(5):
                return
        logger.info(f"mopidy-nowplaying: {self.fb_device} detected! Initializing display...")

        # Once found, perform the actual display initialization
        try:
            with open(f"/sys/class/graphics/{self.framebuffer}/virtual_size") as f:
                width, height = (int(v) for v in f.read().strip().split(","))
            pygame.font.init()
            self._font_file = self._find_font()
            with self._lock:
                self.surfaceSize = (width, height)
                self.scaledSize = (int(width * self.fill_percent), int(height * self.fill_percent))
                self._art_layer = pygame.Surface(self.surfaceSize)
                self._art_layer.fill(BLACK)
                self._art_rect = self._default_art_rect()
                self.screen = pygame.Surface(self.surfaceSize)
            self.render()
        except Exception as e:
            logger.error(f"mopidy-nowplaying: Failed to initialize display after {self.fb_device} appeared: {e}")

    def _find_font(self):
        if self.lyrics_font:
            if os.path.isfile(self.lyrics_font):
                return self.lyrics_font
            logger.warning(f"mopidy-nowplaying: lyrics_font {self.lyrics_font!r} not found")
        # Falls back to pygame's bundled font (Latin only) if nothing matches.
        return pygame.font.match_font("notosans,dejavusans,liberationsans")

    # -- Mopidy events (actor thread: never block here) ------------------

    def track_playback_started(self, tl_track):
        track = tl_track.track
        gen = self._begin_track()
        self._clock.set_position(0)
        self._clock.set_running(True)
        self._executor.submit(self._load_art, gen, track)
        info = TrackInfo.from_track(track)
        if info and self._provider:
            self._executor.submit(self._load_lyrics, gen, info, tl_track)
        self._wake.set()

    def track_playback_paused(self, tl_track, time_position):
        self._clock.set_position(time_position)
        self._clock.set_running(False)
        self._wake.set()

    def track_playback_resumed(self, tl_track, time_position):
        self._clock.set_position(time_position)
        self._clock.set_running(True)
        self._wake.set()

    def seeked(self, time_position):
        self._clock.set_position(time_position)
        self._wake.set()

    def playback_state_changed(self, old_state, new_state):
        state = getattr(new_state, "value", new_state)  # str or enum
        if state == "stopped":
            self._begin_track()
            self._clock.set_running(False)
            self._clock.set_position(0)
            self._blank()
        else:
            self._clock.set_running(state == "playing")
        self._wake.set()

    def _begin_track(self):
        """Invalidate in-flight loads and drop the previous track's lyrics."""
        with self._lock:
            self._generation += 1
            had_lyrics = self._lyrics is not None
            self._lyrics = None
            self._line_index = None
            gen = self._generation
        if had_lyrics:
            self.render()
        return gen

    # -- loaders (worker threads) ----------------------------------------

    def _load_art(self, gen, track):
        data = None
        try:
            url = self._art_url(track)
            if url:
                response = self.session.get(url, timeout=10)
                response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
                data = response.content
        except Exception as e:
            logger.warning(f"mopidy-nowplaying: could not fetch album art: {e}")
        # No art (or a failed fetch) clears the previous album's art on purpose.
        self._set_art(gen, data)

    def _art_url(self, track):
        images = self.core.library.get_images([track.uri]).get().get(track.uri) or ()
        images = list(images)
        art = None
        if len(images) == 1:
            art = images[0].uri
        else:
            for image in images:
                if image.width is None or image.height is None:
                    continue
                if image.height >= 240 and image.width >= 240:
                    art = image.uri
        if art is None:
            return None
        if art.startswith("/"):  # served by mopidy-http (/local/, /youtube/, ...)
            art = f"{self.base_url}{art}"
        if art.startswith(("http://", "https://")):
            return art
        # TODO: What if it isn't a url? What if it's a file?
        return None

    def _load_lyrics(self, gen, info, tl_track):
        lines = self._provider.get(info)
        with self._lock:
            if gen != self._generation:
                return
            if lines:
                self._lyrics = Lyrics(lines)
                self._line_index = None
        if lines:
            self._wake.set()
        else:
            logger.info(f"mopidy-nowplaying: no synced lyrics for {info.title!r}")
        self._prefetch_next(gen, tl_track)

    def _prefetch_next(self, gen, tl_track):
        """Warm the on-disk cache for the next track; LRCLIB can be slow."""
        if gen != self._generation:
            return
        try:
            next_tl = self.core.tracklist.next_track(tl_track).get()
            info = TrackInfo.from_track(next_tl.track if next_tl else None)
            if info:
                self._provider.get(info)
        except Exception:
            logger.debug("mopidy-nowplaying: lyrics prefetch skipped", exc_info=True)

    # -- ticker: redraw only when the current line changes -----------------

    def _run_ticker(self):
        while not self._shutdown.is_set():
            self._wake.clear()
            timeout = None
            try:
                with self._lock:
                    lyrics = self._lyrics
                if lyrics and self._clock.running:
                    position = self._clock.now_ms() - self.lyrics_offset_ms
                    index = lyrics.index_at(position)
                    with self._lock:
                        changed = lyrics is self._lyrics and index != self._line_index
                        if changed:
                            self._line_index = index
                    if changed:
                        self.render()
                    due = lyrics.time_at(index + 1)
                    if due is not None:
                        timeout = max(0.01, (due - position) / 1000)
            except Exception:
                logger.exception("mopidy-nowplaying: lyrics ticker error")
                timeout = 1.0
            # Paused / no lyrics: sleep until an event wakes us.
            self._wake.wait(timeout)

    # -- drawing ------------------------------------------------------------

    def write_framebuffer(self):
        try:
            with open(self.fb_device, "wb") as f:
                f.write(self.screen.get_buffer())
            logger.debug(f"mopidy-nowplaying: Wrote to {self.fb_device}")
        except Exception as e:
            logger.error(f"mopidy-nowplaying: Failed to write to {self.fb_device}: {e}")

    def render(self):
        """Compose art layer + lyrics and push to the framebuffer."""
        with self._lock:
            if self.screen is None:
                return
            try:
                self.screen.blit(self._art_layer, (0, 0))
                self._draw_lyrics()
            except Exception:
                logger.exception("mopidy-nowplaying: render failed")
            self.write_framebuffer()

    def _blank(self):
        with self._lock:
            if self.screen is None:
                return
            self._art_layer.fill(BLACK)
            self._art_rect = self._default_art_rect()
            self.screen.fill(BLACK)
            self.write_framebuffer()

    def _default_art_rect(self):
        rect = pygame.Rect(0, 0, *self.scaledSize)
        rect.center = (self.surfaceSize[0] // 2, self.surfaceSize[1] // 2)
        return rect

    def _set_art(self, gen, data):
        if self.screen is None:
            return
        # Decode and scale outside the lock; it's the slow part.
        layer = pygame.Surface(self.surfaceSize)
        layer.fill(BLACK)
        rect = self._default_art_rect()
        if data:
            try:
                image, rect = self.transformScaleKeepRatio(
                    pygame.image.load(io.BytesIO(data)),
                    self.scaledSize,
                )
                # make near-blacks, black for TV display.
                # I think it looks better like that on a screen.
                pygame.transform.threshold(
                    image,
                    image,
                    search_color=(0, 0, 0),
                    threshold=(25, 25, 25),
                    set_color=(0, 0, 0),
                    set_behavior=1,
                    inverse_set=1,
                )
                layer.blit(image, rect)
            except Exception as e:
                logger.error(f"Failed to update image: {e}")
                rect = self._default_art_rect()
        with self._lock:
            if gen != self._generation:
                return
            self._art_layer, self._art_rect = layer, rect
        self.render()

    def transformScaleKeepRatio(self, image, size):
        iwidth, iheight = image.get_size()
        scale = min(size[0] / iwidth, size[1] / iheight)
        new_size = (round(iwidth * scale), round(iheight * scale))
        scaled_image = pygame.transform.smoothscale(image, new_size)
        image_rect = scaled_image.get_rect(center=(self.surfaceSize[0] // 2, self.surfaceSize[1] // 2))
        return scaled_image, image_rect

    # Lyrics layout: the art is letterboxed in the middle of the screen, so the
    # free bands above and below it hold the text. Previous line (dim) goes in
    # the top band; current (bright) and next (dim) go in the bottom band.

    def _draw_lyrics(self):
        lyrics, index = self._lyrics, self._line_index
        if not (self.lyrics_enabled and lyrics) or index is None:
            return
        width, height = self.surfaceSize
        max_w = int(width * 0.9)

        top_h = max(self._art_rect.top, int(height * 0.1))
        bottom_h = max(height - self._art_rect.bottom, int(height * 0.15))

        top = self._fit([(lyrics.text(index - 1), 0.032, LYRIC_NEIGHBOUR, 2)], max_w, top_h)
        bottom = self._fit(
            [
                (lyrics.text(index), 0.05, LYRIC_CURRENT, 2),
                (lyrics.text(index + 1), 0.032, LYRIC_NEIGHBOUR, 1),
            ],
            max_w,
            bottom_h,
        )
        self._blit_stack(top, 0, top_h)
        self._blit_stack(bottom, height - bottom_h, bottom_h)

    def _fit(self, items, max_w, max_h):
        """Render (text, relative_size, colour, max_lines) items to fit max_h."""
        height = self.surfaceSize[1]
        gap = int(height * 0.012)
        scale = 1.0
        while True:
            blocks = [
                self._text_block(text, max(12, int(height * rel * scale)), colour, max_w, max_lines)
                for text, rel, colour, max_lines in items
                if text
            ]
            total = sum(b.get_height() for b in blocks) + gap * max(0, len(blocks) - 1)
            if total <= max_h or scale < 0.4:
                return blocks
            scale *= 0.85

    def _blit_stack(self, blocks, y, band_h):
        if not blocks:
            return
        gap = int(self.surfaceSize[1] * 0.012)
        total = sum(b.get_height() for b in blocks) + gap * (len(blocks) - 1)
        y += max(0, (band_h - total) // 2)
        for block in blocks:
            self.screen.blit(block, ((self.surfaceSize[0] - block.get_width()) // 2, y))
            y += block.get_height() + gap

    def _text_block(self, text, size, colour, max_w, max_lines):
        """Word-wrapped, centred text as one surface; shrinks to fit max_lines."""
        while True:
            font = self._font(size)
            lines = self._wrap(font, text, max_w)
            if len(lines) <= max_lines or size <= 14:
                break
            size = int(size * 0.9)
        rendered = [font.render(line, True, colour) for line in lines]
        block = pygame.Surface(
            (max(s.get_width() for s in rendered), sum(s.get_height() for s in rendered)),
            pygame.SRCALPHA,
        )
        y = 0
        for surface in rendered:
            block.blit(surface, ((block.get_width() - surface.get_width()) // 2, y))
            y += surface.get_height()
        return block

    def _font(self, size):
        font = self._fonts.get(size)
        if font is None:
            font = self._fonts[size] = pygame.font.Font(self._font_file, size)
        return font

    @staticmethod
    def _wrap(font, text, max_w):
        lines, current = [], ""
        for word in text.split():
            trial = f"{current} {word}".strip()
            if font.size(trial)[0] <= max_w:
                current = trial
                continue
            if current:
                lines.append(current)
                current = ""
            # A single word wider than the screen (or unspaced CJK): split by character.
            while font.size(word)[0] > max_w and len(word) > 1:
                cut = len(word)
                while cut > 1 and font.size(word[:cut])[0] > max_w:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        if current:
            lines.append(current)
        return lines or [text]
