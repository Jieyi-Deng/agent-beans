"""Shared test setup.

Building the 10-second video takes several seconds per ad, so tests skip it (as a machine
without ffmpeg would) unless they are marked ``video``.
"""

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "video: the test builds the ad video with ffmpeg")


@pytest.fixture(autouse=True)
def _no_video_unless_marked(request, monkeypatch):
    if request.node.get_closest_marker("video") is None:
        monkeypatch.setattr("pgt.creative.video.ffmpeg_path", lambda: None)
