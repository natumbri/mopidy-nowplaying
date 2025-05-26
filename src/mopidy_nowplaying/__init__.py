import logging
import pathlib
from importlib.metadata import version

from mopidy import config, ext

__version__ = version("mopidy-nowplaying")

logger = logging.getLogger(__name__)


class Extension(ext.Extension):
    dist_name = "mopidy-nowplaying"
    ext_name = "nowplaying"
    version = __version__

    def get_default_config(self):
        return config.read(pathlib.Path(__file__).parent / "ext.conf")

    def get_config_schema(self):
        schema = super().get_config_schema()
        return schema

    def setup(self, registry):

        from .frontend import NowPlayingFrontend
        registry.add("frontend", NowPlayingFrontend)


