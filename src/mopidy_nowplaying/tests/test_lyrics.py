import time
from unittest.mock import Mock

import requests

from mopidy_nowplaying.lyrics import Line, Lyrics, LyricsProvider, PositionClock, TrackInfo, parse_lrc

SAMPLE = """\
[ar:Someone]
[offset:+500]
[00:12.00]First line
[00:17.5] Second <00:18.00>line
[00:20]
[01:02.345][00:30.1]Chorus
not a lyric line
"""


def test_parse_lrc_basics():
    lines = parse_lrc(SAMPLE)
    assert lines == [
        Line(11500, "First line"),  # 12.000 - 0.5 s offset
        Line(17000, "Second line"),  # .5 s fraction, word tag stripped
        Line(19500, ""),  # kept: instrumental gap
        Line(29600, "Chorus"),  # .1 -> 100 ms; repeated stamp sorted in
        Line(61845, "Chorus"),
    ]


def test_parse_lrc_ignores_metadata_only():
    assert parse_lrc("[ti:Title]\n[ar:Artist]\n") == []


def test_index_at_and_time_at():
    lyrics = Lyrics(parse_lrc("[00:10.00]a\n[00:20.00]b\n[00:30.00]c"))
    assert lyrics.index_at(0) == -1
    assert lyrics.index_at(10_000) == 0
    assert lyrics.index_at(19_999) == 0
    assert lyrics.index_at(25_000) == 1
    assert lyrics.index_at(99_000) == 2
    assert lyrics.time_at(1) == 20_000
    assert lyrics.time_at(3) is None
    assert lyrics.text(-1) == "" and lyrics.text(9) == "" and lyrics.text(2) == "c"


def test_index_at_uses_lead_time_and_word_dwell():
    lyrics = Lyrics(parse_lrc("[00:10.000]one two\n[00:16.000]three\n[00:22.000]four five"))
    assert lyrics.index_at(9_000, lead_ms=1_000) == 0
    assert lyrics.index_at(14_500, lead_ms=1_000) == 0
    assert lyrics.index_at(16_000, lead_ms=1_000) == 1
    assert lyrics.index_at(17_500, lead_ms=1_000) == 1
    assert lyrics.index_at(20_000, lead_ms=1_000) == 1
    assert lyrics.index_at(21_500, lead_ms=1_000) == 2


def test_provider_retries_503_and_caches_success(tmp_path):
    info = TrackInfo("uri", "Title", ("Artist",), "Album", 30)
    session = Mock()
    first = Mock(status_code=503)
    first.raise_for_status.side_effect = requests.HTTPError(response=first)
    second = Mock(status_code=200)
    second.json.return_value = {"syncedLyrics": "[00:10]Hello\n[00:20]World"}
    session.get.side_effect = [first, second]

    provider = LyricsProvider(session, tmp_path)
    lines = provider.get(info)

    assert [line.text for line in lines] == ["Hello", "World"]
    assert provider._read_cache(info.uri) == lines
    assert session.get.call_count == 2


def test_clock_holds_when_paused_and_advances_when_running():
    clock = PositionClock()
    clock.set_position(5_000)
    time.sleep(0.05)
    assert clock.now_ms() == 5_000  # not running yet

    clock.set_running(True)
    time.sleep(0.05)
    assert 5_040 <= clock.now_ms() < 5_500

    clock.set_running(False)
    frozen = clock.now_ms()
    time.sleep(0.05)
    assert clock.now_ms() == frozen

    clock.set_position(1_000)
    assert clock.now_ms() == 1_000
