"""Lossless Bot API entity storage and conversion for the sending account."""
import json

from aiogram.types import MessageEntity as BotEntity
from pyrogram import enums, types


def serialize_entities(entities):
    return json.dumps([entity.model_dump(mode="json", exclude_none=True) for entity in (entities or [])], ensure_ascii=False)


def bot_entities(value):
    return [BotEntity.model_validate(item) for item in json.loads(value or "[]")]


def sender_entities(value):
    result = []
    for item in json.loads(value or "[]"):
        kind = item["type"]
        expandable = kind == "expandable_blockquote"
        enum = getattr(enums.MessageEntityType, "BLOCKQUOTE" if expandable else kind.upper(), None)
        if enum is None:
            continue
        kwargs = {key: item[key] for key in ("offset", "length", "url", "language", "custom_emoji_id", "unix_time", "date_time_format") if key in item}
        if expandable:
            kwargs["expandable"] = True
        if item.get("user"):
            kwargs["user"] = types.User(id=item["user"]["id"], first_name=item["user"].get("first_name", ""))
        result.append(types.MessageEntity(type=enum, **kwargs))
    return result
