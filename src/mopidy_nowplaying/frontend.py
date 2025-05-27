import logging
import os
import threading
import time

import pygame
import pykka
import requests
import io
from mopidy import core

from . import Extension

logger = logging.getLogger(__name__)


class NowPlayingFrontend(pykka.ThreadingActor, core.CoreListener):
    def __init__(self, config, core):
        super().__init__()
        self.core = core
        self.config = config

        # relies on mopidy-http to be configured and running
        self.hostname = self.config.get("http").get("hostname")
        self.port = self.config.get("http").get("port")

        # move hardcoded to config
        self.fill_percent = 0.7
        self.framebuffer_number = 0
        self.framebuffer = f"fb{self.framebuffer_number}"
        self.framebuffer_dimensions = open(
            f"/sys/class/graphics/{self.framebuffer}/virtual_size", "r"
        ).read()

    def write_framebuffer(self):
        with open(f"/dev/{self.framebuffer}", "wb") as f:
            f.write(self.screen.get_buffer())

    def on_start(self):
        self.surfaceSize = tuple(
            int(el) for el in self.framebuffer_dimensions.strip().split(",")
        )
        self.screen = pygame.Surface(self.surfaceSize)
        self.screen.fill((0, 0, 0))
        self.write_framebuffer()

    def on_stop(self):
        self.screen.fill((0, 0, 0))
        self.write_framebuffer()

    def playback_state_changed(self, old_state, new_state):
        if new_state == 'stopped':
            self.screen.fill((0, 0, 0))
            self.write_framebuffer()

    def update_image(self, image_path):
        try:
            image, image_rect = self.transformScaleKeepRatio(
                pygame.image.load(image_path),
                tuple(element * self.fill_percent for element in self.surfaceSize),
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
                response = requests.get(art, stream=True)
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
