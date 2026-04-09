from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from uuid import uuid4

import httpx
from aiogram import Bot as TelegramBot
from vkbottle.bot import Message as VkMessage

from src.models import HomeworkAttachment


class AttachmentStorage:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, storage_path: str | None) -> Path | None:
        if not storage_path:
            return None
        return self.base_dir / storage_path

    def has_local_file(self, attachment: HomeworkAttachment | dict) -> bool:
        storage_path = attachment.storage_path if isinstance(attachment, HomeworkAttachment) else attachment.get("storage_path")
        path = self.resolve_path(storage_path)
        return bool(path and path.exists())

    def delete_attachments(self, attachments: list[HomeworkAttachment | dict]) -> None:
        for attachment in attachments:
            storage_path = attachment.storage_path if isinstance(attachment, HomeworkAttachment) else attachment.get("storage_path")
            path = self.resolve_path(storage_path)
            if path is None or not path.exists():
                continue
            path.unlink(missing_ok=True)

    async def save_telegram_file(
        self,
        bot: TelegramBot,
        file_id: str,
        file_type: str,
        file_name: str | None,
        mime_type: str | None,
    ) -> HomeworkAttachment:
        relative_path = self._build_relative_path("telegram", file_type, file_name, mime_type)
        target_path = self.base_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        await bot.download(file_id, destination=target_path)
        return HomeworkAttachment(
            file_id=file_id,
            file_type=file_type,
            file_name=file_name or target_path.name,
            mime_type=mime_type,
            storage_path=relative_path.as_posix(),
            source_platform="telegram",
        )

    async def save_vk_url(
        self,
        url: str,
        file_type: str,
        file_name: str | None,
        mime_type: str | None,
        file_id: str = "",
    ) -> HomeworkAttachment:
        relative_path = self._build_relative_path("vk", file_type, file_name, mime_type)
        target_path = self.base_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            response = await client.get(url)
            response.raise_for_status()
        target_path.write_bytes(response.content)
        return HomeworkAttachment(
            file_id=file_id,
            file_type=file_type,
            file_name=file_name or target_path.name,
            mime_type=mime_type,
            storage_path=relative_path.as_posix(),
            source_platform="vk",
        )

    async def save_vk_message_attachments(self, full_message: VkMessage) -> list[HomeworkAttachment]:
        saved: list[HomeworkAttachment] = []
        for message_attachment in full_message.attachments or []:
            attachment_type = message_attachment.type.value
            attachment_object = getattr(message_attachment, attachment_type, None)
            attachment_string = self._build_vk_attachment_string(attachment_type, attachment_object)

            if attachment_type == "photo" and message_attachment.photo:
                url = self._pick_vk_photo_url(message_attachment.photo)
                if not url:
                    continue
                saved.append(
                    await self.save_vk_url(
                        url=url,
                        file_type="photo",
                        file_name=f"photo_{message_attachment.photo.id}.jpg",
                        mime_type="image/jpeg",
                        file_id=attachment_string,
                    )
                )
                continue

            if attachment_type == "doc" and message_attachment.doc and message_attachment.doc.url:
                file_type = self._vk_doc_type(message_attachment.doc.ext or "")
                mime_type = self._guess_mime_type(message_attachment.doc.title, message_attachment.doc.ext)
                saved.append(
                    await self.save_vk_url(
                        url=message_attachment.doc.url,
                        file_type=file_type,
                        file_name=message_attachment.doc.title,
                        mime_type=mime_type,
                        file_id=attachment_string,
                    )
                )
                continue

            if attachment_type == "audio_message" and message_attachment.audio_message:
                saved.append(
                    await self.save_vk_url(
                        url=message_attachment.audio_message.link_mp3,
                        file_type="document",
                        file_name=f"voice_{message_attachment.audio_message.id}.mp3",
                        mime_type="audio/mpeg",
                        file_id=attachment_string,
                    )
                )
                continue

            if attachment_type == "audio" and message_attachment.audio and message_attachment.audio.url:
                saved.append(
                    await self.save_vk_url(
                        url=message_attachment.audio.url,
                        file_type="audio",
                        file_name=f"{message_attachment.audio.artist} - {message_attachment.audio.title}.mp3",
                        mime_type="audio/mpeg",
                        file_id=attachment_string,
                    )
                )
                continue

            if attachment_type == "video" and message_attachment.video:
                url = self._pick_vk_video_url(message_attachment.video)
                if not url:
                    continue
                saved.append(
                    await self.save_vk_url(
                        url=url,
                        file_type="video",
                        file_name=f"video_{message_attachment.video.id}.mp4",
                        mime_type="video/mp4",
                        file_id=attachment_string,
                    )
                )

        return saved

    def _build_relative_path(
        self,
        source_platform: str,
        file_type: str,
        file_name: str | None,
        mime_type: str | None,
    ) -> Path:
        extension = self._detect_extension(file_name, mime_type, file_type)
        safe_name = self._sanitize_stem(Path(file_name).stem if file_name else file_type)
        unique_name = f"{uuid4().hex}_{safe_name}{extension}"
        return Path(source_platform) / unique_name

    def _detect_extension(self, file_name: str | None, mime_type: str | None, file_type: str) -> str:
        if file_name:
            suffix = Path(file_name).suffix
            if suffix:
                return suffix
        if mime_type:
            guessed = mimetypes.guess_extension(mime_type)
            if guessed:
                return guessed
        return {
            "photo": ".jpg",
            "video": ".mp4",
            "audio": ".mp3",
            "document": ".bin",
        }.get(file_type, ".bin")

    def _sanitize_stem(self, stem: str) -> str:
        clean = re.sub(r"[^A-Za-zА-Яа-я0-9._-]+", "_", stem.strip())
        return clean[:64] or "file"

    def _build_vk_attachment_string(self, attachment_type: str, attachment_object) -> str:
        if attachment_object is None:
            return ""
        if not hasattr(attachment_object, "id") or not hasattr(attachment_object, "owner_id"):
            return ""
        attachment_string = f"{attachment_type}{attachment_object.owner_id}_{attachment_object.id}"
        access_key = getattr(attachment_object, "access_key", None)
        if access_key:
            attachment_string += f"_{access_key}"
        return attachment_string

    def _pick_vk_photo_url(self, photo) -> str | None:
        if getattr(photo, "orig_photo", None) and getattr(photo.orig_photo, "url", None):
            return photo.orig_photo.url
        candidates = []
        for collection_name in ("sizes", "images"):
            for item in getattr(photo, collection_name, None) or []:
                url = getattr(item, "url", None)
                width = getattr(item, "width", 0) or 0
                height = getattr(item, "height", 0) or 0
                if url:
                    candidates.append((width * height, url))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]
        return getattr(photo, "photo_256", None)

    def _pick_vk_video_url(self, video) -> str | None:
        files = getattr(video, "files", None)
        if files is None:
            return None
        for field_name in ("mp4_2160", "mp4_1440", "mp4_1080", "mp4_720", "mp4_480", "mp4_360", "mp4_240", "mp4_144"):
            url = getattr(files, field_name, None)
            if url:
                return url
        return None

    def _vk_doc_type(self, extension: str) -> str:
        normalized = extension.lower().lstrip(".")
        if normalized in {"jpg", "jpeg", "png", "gif", "webp"}:
            return "photo"
        if normalized in {"mp3", "wav", "m4a", "ogg"}:
            return "audio"
        if normalized in {"mp4", "mov", "avi", "mkv", "webm"}:
            return "video"
        return "document"

    def _guess_mime_type(self, file_name: str | None, extension: str | None) -> str | None:
        if file_name:
            guessed, _ = mimetypes.guess_type(file_name)
            if guessed:
                return guessed
        if extension:
            guessed, _ = mimetypes.guess_type(f"file.{extension.lstrip('.')}")
            if guessed:
                return guessed
        return None
