"""Сериализация API-запросов, результатов и неизменяемых копий вложений."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiogram.methods
from aiogram.client.default import Default
from aiogram.types import BufferedInputFile, FSInputFile, URLInputFile
from pydantic import BaseModel, TypeAdapter

_FILE_MARKER = "__outbox_file__"


def json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        # Модели ответа SDK тоже имеют Default-сентинелы (LinkPreviewOptions).
        # Сохраняем полученные поля API, исключая незаданные настройки SDK.
        return value.model_dump(mode="json", exclude_none=True, exclude_unset=True)
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def telegram_method(name: str, payload: dict):
    for method_type in vars(aiogram.methods).values():
        if isinstance(method_type, type) and getattr(method_type, "__api_method__", None) == name:
            return method_type.model_validate(payload)
    raise ValueError(f"Unknown Telegram method: {name}")


def telegram_result(method, value, bot):
    return TypeAdapter(method.__returning__).validate_python(value, context={"bot": bot})


class OutboxFiles:
    def __init__(self, root: Path, database_path: Path) -> None:
        self.root = root.resolve()
        self.database_path = database_path.resolve()

    async def encode(self, value, bot=None) -> Any:
        if isinstance(value, Default):
            return await self.encode(getattr(bot.default, value.name, None), bot) if bot is not None else None
        if isinstance(value, (FSInputFile, BufferedInputFile)):
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = uuid4().hex
            path = self.root / name

            def copy() -> None:
                if isinstance(value, BufferedInputFile):
                    path.write_bytes(value.data)
                elif Path(value.path).resolve() == self.database_path:
                    # Копирование одного .db без WAL не является корректным бэкапом.
                    with (closing(sqlite3.connect(f"file:{self.database_path.as_posix()}?mode=ro", uri=True)) as source,
                          closing(sqlite3.connect(path)) as target):
                        source.backup(target)
                else:
                    shutil.copyfile(value.path, path)

            await asyncio.to_thread(copy)
            return {_FILE_MARKER: "local", "name": name, "filename": value.filename, "chunk_size": value.chunk_size}
        if isinstance(value, URLInputFile):
            return {
                _FILE_MARKER: "url", "url": value.url, "headers": value.headers,
                "filename": value.filename, "chunk_size": value.chunk_size, "timeout": value.timeout,
            }
        if isinstance(value, BaseModel):
            return await self.encode(value.model_dump(mode="python", exclude_none=True, warnings=False), bot)
        if isinstance(value, dict):
            return {str(key): await self.encode(item, bot) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [await self.encode(item, bot) for item in value]
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError(f"Cannot persist API parameter {type(value).__name__}")

    def decode(self, value) -> Any:
        if isinstance(value, dict):
            if value.get(_FILE_MARKER) == "local":
                name = str(value["name"])
                if len(name) != 32 or any(char not in "0123456789abcdef" for char in name):
                    raise ValueError("Invalid outbox attachment name")
                path = self.root / name
                if path.resolve().parent != self.root:
                    raise ValueError("Outbox attachment escaped storage")
                return FSInputFile(path, filename=value.get("filename"), chunk_size=value.get("chunk_size", 65536))
            if value.get(_FILE_MARKER) == "url":
                return URLInputFile(**{key: item for key, item in value.items() if key != _FILE_MARKER})
            return {key: self.decode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.decode(item) for item in value]
        return value

    def fingerprint(self, value) -> str:
        """Файлы с одинаковым содержимым дают один ключ при повторе входящего события."""
        def stable(item):
            if isinstance(item, dict):
                if item.get(_FILE_MARKER) == "local":
                    digest = hashlib.sha256()
                    with (self.root / item["name"]).open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    return {"file_hash": digest.hexdigest(), "filename": item.get("filename")}
                return {key: stable(child) for key, child in item.items()}
            if isinstance(item, list):
                return [stable(child) for child in item]
            return item
        return hashlib.sha256(json.dumps(stable(value), ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def remove_payload_files(self, value) -> None:
        if isinstance(value, dict):
            if value.get(_FILE_MARKER) == "local":
                item = self.decode(value)
                Path(item.path).unlink(missing_ok=True)
            else:
                for child in value.values():
                    self.remove_payload_files(child)
        elif isinstance(value, list):
            for child in value:
                self.remove_payload_files(child)
