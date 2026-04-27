import logging

import pygame
import pykka
import requests
import io
from mopidy import core, httpclient

import threading
import time
import os

from . import Extension

logger = logging.getLogger(__name__)


class NowPlayingFrontend(pykka.ThreadingActor, core.CoreListener):
    def __init__(self, config, core):
        super().__init__()
        self.core = core
        self.config = config
        logger.info(f'nowplaying: {config.get('proxy')}')
        # relies on mopidy-http to be configured and running
        self.hostname = self.config.get("http").get("hostname")
        self.port = self.config.get("http").get("port")

        # Initialize a standard Requests session
        self.session = requests.Session()
        # self.session.proxies.update(httpclient.format_proxy(config.get('proxy', {})))
        self.session.headers.update({'User-Agent': httpclient.format_user_agent()})

        # move hardcoded to config
        self.fill_percent = 0.7
        self.framebuffer_number = 0
        self.framebuffer = f"fb{self.framebuffer_number}"
        self.fb_device = f"/dev/{self.framebuffer}"
        self.framebuffer_dimensions = None
        
    def write_framebuffer(self):
        try:
            with open(self.fb_device, "wb") as f:
                f.write(self.screen.get_buffer())
            logger.debug(f"mopidy-nowplaying: Wrote to {self.fb_device}")
        except Exception as e:
            logger.error(f"mopidy-nowplaying: Failed to write to {self.fb_device}: {e}")

    def on_start(self):
        # Spawn a background thread so Mopidy can continue loading
        self.init_thread = threading.Thread(target=self._bg_wait_for_display, daemon=True)
        self.init_thread.start()

    def _bg_wait_for_display(self):
        
        # Indefinite loop: Check every 5 seconds if fb0 exists
        while not os.path.exists(self.fb_device):
            logger.debug(f"mopidy-nowplaying: {self.fb_device} not found. Waiting...")
            time.sleep(5)

        logger.info(f"mopidy-nowplaying: {self.fb_device} detected! Initializing display...")
        
        # Once found, perform the actual display initialization
        try:
            self.framebuffer_dimensions = open(
                f"/sys/class/graphics/{self.framebuffer}/virtual_size", "r"
            ).read()

            self.surfaceSize = tuple(
                int(el) for el in self.framebuffer_dimensions.strip().split(",")
            )
            self.scaledSize = tuple(element * self.fill_percent for element in self.surfaceSize)
            self.screen = pygame.Surface(self.surfaceSize)
            self.screen.fill((0, 0, 0))
            self.write_framebuffer()
        except Exception as e:
            logger.error(f"mopidy-nowplaying: Failed to initialize display after {self.fb_device} appeared: {e}")

    def on_stop(self):
        self.screen.fill((0, 0, 0))
        self.write_framebuffer()

    def playback_state_changed(self, old_state, new_state):
        if new_state == "stopped":
            self.screen.fill((0, 0, 0))
            self.write_framebuffer()

    def update_image(self, image_path):
        try:
            image, image_rect = self.transformScaleKeepRatio(
                pygame.image.load(image_path),
                self.scaledSize,
            )

            # make near-blacks, black for TV display. 
            # I think it looks better like that on a screen. 
            pygame.transform.threshold(
                image,
                image,
                search_color=(0,0,0),
                threshold=(25,25,25),
                set_color=(0,0,0),
                set_behavior=1,
                inverse_set=1,
            )
            self.screen.fill((0, 0, 0))
            self.screen.blit(image, image_rect)
            self.write_framebuffer()

        except Exception as e:
            logger.error(f"Failed to update image: {e}")

    def track_playback_started(self, tl_track):
        (tlid, track) = tl_track
        self.update_track(track)

    def update_track(self, track, time_position=None):
        if track is None:
            track = self.core.playback.get_current_track().get()

        art = None
        track_images = self.core.library.get_images([track.uri]).get()
        if track.uri in track_images:
            track_images = track_images[track.uri]
            if len(track_images) == 1:
                art = track_images[0].uri
            else:
                for image in track_images:
                    if image.width is None or image.height is None:
                        continue
                    if image.height >= 240 and image.width >= 240:
                        art = image.uri

        self.update_album_art(art)

    def update_album_art(self, art=None):
        if art is not None:
            if art.startswith("/local/") or art.startswith("/youtube/"):
                art = f"http://{self.hostname}:{self.port}{art}"

            if art.startswith("http://") or art.startswith("https://"):
                response = self.session.get(art, stream=True)
                response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
                self.update_image(io.BytesIO(response.content))

            # TODO: What if it isn't a url?  What if it's a file?

    def transformScaleKeepRatio(self, image, size):
        iwidth, iheight = image.get_size()
        scale = min(size[0] / iwidth, size[1] / iheight)
        new_size = (round(iwidth * scale), round(iheight * scale))
        scaled_image = pygame.transform.smoothscale(image, new_size)
        image_rect = scaled_image.get_rect(center=self.screen.get_rect().center)
        return scaled_image, image_rect
