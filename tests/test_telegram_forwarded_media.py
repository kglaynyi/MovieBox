"""Telegram forwarded/captioned video title selection regressions."""
from types import SimpleNamespace
from unittest import TestCase

from Backend.helper.message_media import media_message_title


def message(filename, caption):
    return SimpleNamespace(
        video=SimpleNamespace(file_name=filename), document=None, caption=caption,
        forward_origin=SimpleNamespace(type="channel"),
    )


class ForwardedMediaTitleTests(TestCase):
    def test_forwarded_caption_does_not_hide_scene_filename(self):
        msg = message("Blade.Runner.2049.2017.2160p.mkv", "🔥 Download now\nJoin @example")
        self.assertEqual(media_message_title(msg), "Blade.Runner.2049.2017.2160p.mkv")

    def test_generic_filename_uses_useful_caption(self):
        msg = message("video_1234.mp4", "Dune Part Two (2024) 1080p")
        self.assertEqual(media_message_title(msg), "Dune Part Two (2024) 1080p")

    def test_filename_without_caption_is_kept(self):
        self.assertEqual(media_message_title(message("Movie.2025.mkv", None)), "Movie.2025.mkv")
