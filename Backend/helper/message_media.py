"""Select stable filenames from Telegram media messages."""
import re

from Backend.helper.settings_manager import SettingsManager


_GENERIC_FILE_STEM = re.compile(
    r"^(?:video|vid|file|document|movie|telegram)[-_ ]*\d*$", re.IGNORECASE
)


def media_message_title(message) -> str:
    """Prefer a real media filename; use the caption for generic/unnamed uploads.

    Forwarded channel posts commonly keep a descriptive caption which is not a
    Scene-style title. Parsing that caption instead of the attached filename
    makes otherwise valid forwarded videos disappear during metadata matching.
    """
    media = getattr(message, "video", None) or getattr(message, "document", None)
    filename = str(getattr(media, "file_name", "") or "").strip()
    caption = str(getattr(message, "caption", "") or "").strip()
    if filename:
        stem = filename.rsplit(".", 1)[0].strip()
        if not _GENERIC_FILE_STEM.fullmatch(stem):
            return filename
    return caption or filename


def telegram_scene_filename(channel) -> bool:
    """Use Scene parsing for Telegram except where absolute anime episodes are expected."""
    target = str(channel).replace("-100", "")
    anime_channels = SettingsManager.current().anime_channels
    return not any(
        str(item).strip().replace("-100", "") == target
        for item in anime_channels
    )
