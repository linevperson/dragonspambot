"""Explicit group-local permission errors, not arbitrary account restrictions."""
from pyrogram import errors


MEDIA_ERRORS = {
    errors.ChatSendVideosForbidden: ({"video"}, "В группе запрещены видео"),
    errors.ChatSendRoundvideosForbidden: ({"video_note"}, "В группе запрещены кружки"),
    errors.ChatSendPhotosForbidden: ({"photo"}, "В группе запрещены фотографии"),
    errors.ChatSendMediaForbidden: ({"photo", "video", "video_note"}, "В группе запрещены медиафайлы"),
    errors.ChatSendPlainForbidden: ({"text"}, "В группе запрещены текстовые сообщения"),
}


def media_restriction(exc):
    for kind, result in MEDIA_ERRORS.items():
        if isinstance(exc, kind):
            return result
    return None
