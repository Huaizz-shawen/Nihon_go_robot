"""QQ Bot REST helpers used by the Codex bridge."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx


API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_TEXT_SAFE_LIMIT = 1400
LONG_REPLY_NOTICE = "📄 回复内容较长，已转为 TXT 文件发送。"

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}
_FILE_EXT = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".zip", ".tar", ".gz",
    ".7z", ".csv", ".py", ".json", ".md", ".log", ".html", ".css",
    ".sh", ".yaml", ".yml", ".toml", ".cfg", ".svg", ".bmp", ".gif",
    ".tiff", ".mp4", ".mp3",
}
_IMAGE_MAX = 10 * 1024 * 1024
_FILE_MAX = 100 * 1024 * 1024
_BIZ_CODE_RETRYABLE = 40093001
_ATTACHMENT_IMAGE_MAX = 10 * 1024 * 1024
_ATTACHMENT_AUDIO_MAX = 25 * 1024 * 1024
_PROXY_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
)
_QQ_CDN_FAKE_IP_HOSTS = frozenset(
    {
        "multimedia.nt.qq.com.cn",
        "grouptalk.c2c.qq.com",
        "qqbot.ugcimg.cn",
    }
)
_SEND_IMG_RE = re.compile(r"\[\[SEND_IMAGE:(/.+?)\]\]")
_SEND_FILE_RE = re.compile(r"\[\[SEND_FILE:(/.+?)\]\]")


def build_approval_keyboard(token: str) -> dict[str, Any]:
    """Build a keyboard whose opaque token maps to one pending App Server request."""
    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        {
                            "id": f"allow_{token}",
                            "render_data": {
                                "label": "✅ 允许一次",
                                "visited_label": "已允许",
                                "style": 1,
                            },
                            "action": {
                                "type": 2,
                                "permission": {"type": 2},
                                "data": f"codex-approve:{token}:accept",
                            },
                        },
                        {
                            "id": f"always_{token}",
                            "render_data": {
                                "label": "🛡️ 本会话允许",
                                "visited_label": "本会话已允许",
                                "style": 1,
                            },
                            "action": {
                                "type": 2,
                                "permission": {"type": 2},
                                "data": f"codex-approve:{token}:acceptForSession",
                            },
                        },
                    ]
                },
                {
                    "buttons": [
                        {
                            "id": f"deny_{token}",
                            "render_data": {
                                "label": "❌ 拒绝",
                                "visited_label": "已拒绝",
                                "style": 0,
                            },
                            "action": {
                                "type": 2,
                                "permission": {"type": 2},
                                "data": f"codex-approve:{token}:decline",
                            },
                        }
                    ]
                },
            ]
        }
    }


def compute_file_hashes(file_path: str) -> dict[str, Any]:
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    md5_10m = hashlib.md5()
    bytes_read = 0
    with open(file_path, "rb") as source:
        while chunk := source.read(8192):
            md5.update(chunk)
            sha1.update(chunk)
            if bytes_read < 10_002_432:
                md5_10m.update(chunk[: 10_002_432 - bytes_read])
            bytes_read += len(chunk)
    return {
        "md5": md5.hexdigest(),
        "sha1": sha1.hexdigest(),
        "md5_10m": md5_10m.hexdigest(),
        "total_size": bytes_read,
    }


def validate_local_file(file_path: str, max_size: int) -> tuple[bool, str]:
    path = Path(file_path).expanduser()
    if not path.exists():
        return False, f"文件不存在: {path}"
    if not path.is_file():
        return False, f"不是普通文件: {path}"
    allowed = _IMAGE_EXT if max_size == _IMAGE_MAX else (_IMAGE_EXT | _FILE_EXT)
    if path.suffix.lower() not in allowed:
        return False, f"不支持的文件类型: {path.suffix.lower()}"
    if path.stat().st_size > max_size:
        return False, f"文件过大，限制 {max_size // (1024 * 1024)}MB"
    return True, ""


def extract_media_markers(text: str) -> tuple[str, list[dict[str, str]]]:
    media: list[dict[str, str]] = []
    media.extend({"type": "image", "path": m.group(1)} for m in _SEND_IMG_RE.finditer(text))
    media.extend({"type": "file", "path": m.group(1)} for m in _SEND_FILE_RE.finditer(text))
    clean = _SEND_IMG_RE.sub("", text)
    clean = _SEND_FILE_RE.sub("", clean)
    return re.sub(r"\n{3,}", "\n\n", clean).strip(), media


class QQApi:
    """Authenticated QQ Bot REST client with text and media support."""

    def __init__(
        self,
        app_id: str,
        client_secret: str,
        *,
        logger: logging.Logger | None = None,
        temp_dir: Path | None = None,
        trust_env_proxy: bool = False,
    ) -> None:
        self.app_id = app_id
        self.client_secret = client_secret
        self.logger = logger or logging.getLogger(__name__)
        self.temp_dir = temp_dir or Path("/tmp/codex-qq-bridge")
        self.trust_env_proxy = trust_env_proxy
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._client: httpx.AsyncClient | None = None
        self._msg_seq: dict[str, int] = {}

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                trust_env=self.trust_env_proxy,
            )
        return self._client

    def _next_seq(self, target: str) -> int:
        value = self._msg_seq.get(target, 0) + 1
        if len(self._msg_seq) > 1000:
            self._msg_seq.clear()
            value = 1
        self._msg_seq[target] = value
        return value

    async def token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        if not self.app_id or not self.client_secret:
            raise RuntimeError("APP_ID 和 CLIENT_SECRET 尚未配置")
        response = await self._http().post(
            TOKEN_URL,
            json={"appId": self.app_id, "clientSecret": self.client_secret},
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError("QQ Bot access token 响应缺少 access_token")
        self._access_token = str(token)
        self._token_expires_at = time.time() + int(data.get("expires_in", 7200))
        return self._access_token

    async def gateway_url(self) -> str:
        token = await self.token()
        response = await self._http().get(
            f"{API_BASE}/gateway",
            headers={
                "Authorization": f"QQBot {token}",
                "User-Agent": "Codex-QQ-Bridge/2.0",
            },
        )
        response.raise_for_status()
        url = response.json().get("url")
        if not url:
            raise RuntimeError("QQ gateway 响应缺少 url")
        return str(url)

    @staticmethod
    async def _validate_attachment_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("附件地址必须是 HTTPS")
        if parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ValueError("附件地址包含不允许的认证信息或端口")
        try:
            literal = ipaddress.ip_address(parsed.hostname)
            addresses = [literal]
        except ValueError:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                443,
                type=socket.SOCK_STREAM,
            )
            addresses = list({ipaddress.ip_address(record[4][0]) for record in records})
        fake_ip_allowed = (
            parsed.hostname.lower() in _QQ_CDN_FAKE_IP_HOSTS
            and addresses
            and all(
                any(address in network for network in _PROXY_FAKE_IP_NETWORKS)
                for address in addresses
            )
        )
        if not addresses or (
            not fake_ip_allowed and any(not address.is_global for address in addresses)
        ):
            raise ValueError("附件地址不能指向本机或私有网络")

    async def fetch_attachment_data_url(self, url: str, declared_type: str) -> str:
        """Download a QQ media attachment safely and return an inline data URL."""
        kind = declared_type.lower().split(";", 1)[0].strip()
        if kind.startswith("image/"):
            limit, expected = _ATTACHMENT_IMAGE_MAX, "image/"
        elif kind.startswith("audio/") or "voice" in kind:
            limit, expected = _ATTACHMENT_AUDIO_MAX, "audio/"
        else:
            raise ValueError("只支持将图片和音频转换为 Codex 内联附件")

        current = url
        for _ in range(4):
            await self._validate_attachment_url(current)
            hostname = (urlsplit(current).hostname or "").lower()
            headers = {
                "Accept": "*/*",
                "User-Agent": "Codex-QQ-Bridge/2.0",
            }
            if hostname == "multimedia.nt.qq.com.cn":
                headers["Authorization"] = f"QQBot {await self.token()}"
            async with self._http().stream(
                "GET",
                current,
                headers=headers,
                follow_redirects=False,
                timeout=30.0,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("附件重定向缺少 Location")
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    raise RuntimeError(f"附件下载失败: HTTP {response.status_code}")
                length = response.headers.get("content-length")
                if length and int(length) > limit:
                    raise ValueError("附件超过允许大小")
                response_type = response.headers.get("content-type", "")
                mime = response_type.split(";", 1)[0].strip().lower() or kind
                if mime != "application/octet-stream" and not mime.startswith(expected):
                    raise ValueError("附件响应类型与声明类型不一致")
                if mime == "application/octet-stream":
                    mime = kind
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > limit:
                        raise ValueError("附件超过允许大小")
                if not payload:
                    raise ValueError("附件内容为空")
                encoded = base64.b64encode(payload).decode("ascii")
                return f"data:{mime};base64,{encoded}"
        raise RuntimeError("附件重定向次数过多")

    async def send_typing(self, openid: str, msg_id: str) -> bool:
        token = await self.token()
        body = {
            "msg_type": 6,
            "input_notify": {"input_type": 1, "input_second": 10},
            "msg_seq": self._next_seq(openid),
            "msg_id": msg_id,
        }
        try:
            response = await self._http().post(
                f"{API_BASE}/v2/users/{openid}/messages",
                headers={"Authorization": f"QQBot {token}"},
                json=body,
            )
            return response.status_code < 400
        except Exception as exc:
            self.logger.warning("QQ typing notification failed: %s", exc)
            return False

    async def acknowledge_interaction(self, interaction_id: str) -> None:
        token = await self.token()
        response = await self._http().put(
            f"{API_BASE}/interactions/{interaction_id}",
            headers={"Authorization": f"QQBot {token}"},
            json={"code": 0},
            timeout=5,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"QQ interaction ACK failed: HTTP {response.status_code}")

    async def send_text(
        self, openid: str, content: str, *, keyboard: dict[str, Any] | None = None
    ) -> bool:
        token = await self.token()
        body: dict[str, Any] = {
            "markdown": {"content": content},
            "msg_type": 2,
            "msg_seq": self._next_seq(openid),
        }
        if keyboard:
            body["keyboard"] = keyboard
        try:
            response = await self._http().post(
                f"{API_BASE}/v2/users/{openid}/messages",
                headers={
                    "Authorization": f"QQBot {token}",
                    "Content-Type": "application/json",
                    "User-Agent": "Codex-QQ-Bridge/2.0",
                },
                json=body,
            )
            if response.status_code >= 400:
                self.logger.error(
                    "QQ send failed [%s]: %s", response.status_code, response.text[:300]
                )
                return False
            return True
        except Exception as exc:
            self.logger.error("QQ send exception: %s", exc)
            return False

    async def send_group_text(
        self,
        group_openid: str,
        content: str,
        *,
        msg_id: str = "",
        keyboard: dict[str, Any] | None = None,
    ) -> bool:
        """Send a plain-text passive reply to a QQ group."""
        token = await self.token()
        body: dict[str, Any] = {
            "content": content[:QQ_TEXT_SAFE_LIMIT],
            "msg_type": 0,
            "msg_seq": self._next_seq(group_openid),
        }
        if msg_id:
            body["msg_id"] = msg_id
            body["message_reference"] = {"message_id": msg_id}
        if keyboard:
            body["keyboard"] = keyboard
        try:
            response = await self._http().post(
                f"{API_BASE}/v2/groups/{group_openid}/messages",
                headers={
                    "Authorization": f"QQBot {token}",
                    "Content-Type": "application/json",
                    "User-Agent": "Codex-QQ-Bridge/2.0",
                },
                json=body,
            )
            if response.status_code >= 400:
                self.logger.error(
                    "QQ group send failed [%s]: %s",
                    response.status_code,
                    response.text[:300],
                )
                return False
            return True
        except Exception as exc:
            self.logger.error("QQ group send exception: %s", exc)
            return False

    async def send_group_reply(
        self, group_openid: str, content: str, *, msg_id: str = ""
    ) -> bool:
        """Send long group replies as bounded plain-text chunks."""
        if not content.strip():
            return True
        chunks = [
            content[index : index + QQ_TEXT_SAFE_LIMIT]
            for index in range(0, len(content), QQ_TEXT_SAFE_LIMIT)
        ]
        for chunk in chunks:
            if not await self.send_group_text(group_openid, chunk, msg_id=msg_id):
                return False
        return True

    async def send_reply(
        self, openid: str, content: str, *, keyboard: dict[str, Any] | None = None
    ) -> bool:
        if not content.strip():
            return True
        if keyboard or len(content) <= QQ_TEXT_SAFE_LIMIT:
            return await self.send_text(openid, content, keyboard=keyboard)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        temp_path = self.temp_dir / f"codex_reply_{stamp}.txt"
        temp_path.write_text(content, encoding="utf-8")
        temp_path.chmod(0o600)
        await self.send_text(openid, LONG_REPLY_NOTICE)
        try:
            result = await self.send_local_file(str(temp_path), openid)
            return result.startswith("✅")
        finally:
            temp_path.unlink(missing_ok=True)

    async def _upload_file(
        self,
        file_path: str,
        file_type: int,
        openid: str,
        *,
        is_group: bool = False,
    ) -> str | None:
        token = await self.token()
        path = Path(file_path).expanduser().resolve()
        hashes = await asyncio.to_thread(compute_file_hashes, str(path))
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
        }
        target_path = "groups" if is_group else "users"
        prepare = await self._http().post(
            f"{API_BASE}/v2/{target_path}/{openid}/upload_prepare",
            headers=headers,
            json={
                "file_type": file_type,
                "file_size": str(path.stat().st_size),
                "file_name": path.name,
                "md5": hashes["md5"],
                "sha1": hashes["sha1"],
                "md5_10m": hashes["md5_10m"],
            },
        )
        if prepare.status_code >= 400:
            self.logger.error("QQ upload prepare failed: %s", prepare.text[:300])
            return None
        data = prepare.json()
        if isinstance(data.get("data"), dict):
            data = data["data"]
        upload_id = data.get("upload_id")
        parts = data.get("parts") or data.get("part_list") or []
        block_size = int(data.get("block_size", 5_242_880))
        if not upload_id or not parts:
            return None
        offset = 0
        async with httpx.AsyncClient(
            timeout=120.0,
            follow_redirects=True,
            trust_env=self.trust_env_proxy,
        ) as upload_client:
            with path.open("rb") as source:
                for part in parts:
                    api_index = int(part.get("index", part.get("part_index", 0)))
                    size = int(part.get("block_size", block_size))
                    source.seek(offset)
                    chunk = source.read(size)
                    if not chunk:
                        return None
                    put_response = await upload_client.put(
                        str(part.get("presigned_url", "")),
                        content=chunk,
                        headers={"Content-Length": str(len(chunk))},
                    )
                    if put_response.status_code >= 300:
                        return None
                    finish_body = {
                        "upload_id": upload_id,
                        "part_index": api_index,
                        "block_size": str(len(chunk)),
                        "md5": hashlib.md5(chunk).hexdigest(),
                    }
                    deadline = time.time() + 120
                    while True:
                        finish = await upload_client.post(
                            f"{API_BASE}/v2/{target_path}/{openid}/upload_part_finish",
                            headers=headers,
                            json=finish_body,
                        )
                        if finish.status_code < 400:
                            break
                        try:
                            code = finish.json().get("biz_code") or finish.json().get("code")
                        except Exception:
                            code = None
                        if code == _BIZ_CODE_RETRYABLE and time.time() < deadline:
                            await asyncio.sleep(1)
                            continue
                        return None
                    offset += len(chunk)
        finalized = await self._http().post(
            f"{API_BASE}/v2/{target_path}/{openid}/files",
            headers=headers,
            json={"file_type": file_type, "upload_id": upload_id},
        )
        if finalized.status_code >= 400:
            return None
        result = finalized.json()
        if isinstance(result.get("data"), dict):
            result = result["data"]
        return result.get("file_info") or result.get("file_uuid")

    async def _send_media(
        self,
        file_info: str,
        openid: str,
        *,
        is_group: bool = False,
        msg_id: str = "",
    ) -> bool:
        token = await self.token()
        target_path = "groups" if is_group else "users"
        body: dict[str, Any] = {
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_seq": self._next_seq(openid),
        }
        if msg_id:
            body["msg_id"] = msg_id
            body["message_reference"] = {"message_id": msg_id}
        response = await self._http().post(
            f"{API_BASE}/v2/{target_path}/{openid}/messages",
            headers={"Authorization": f"QQBot {token}"},
            json=body,
        )
        return response.status_code < 400

    async def send_local_image(
        self,
        file_path: str,
        openid: str,
        *,
        is_group: bool = False,
        msg_id: str = "",
    ) -> str:
        ok, error = validate_local_file(file_path, _IMAGE_MAX)
        if not ok:
            return f"❌ {error}"
        file_info = await self._upload_file(
            file_path, 1, openid, is_group=is_group
        )
        if file_info and await self._send_media(
            file_info, openid, is_group=is_group, msg_id=msg_id
        ):
            return f"✅ 图片已发送: {Path(file_path).name}"
        return f"❌ 图片发送失败: {Path(file_path).name}"

    async def send_local_file(
        self, file_path: str, openid: str, *, is_group: bool = False
    ) -> str:
        ok, error = validate_local_file(file_path, _FILE_MAX)
        if not ok:
            return f"❌ {error}"
        file_type = 1 if Path(file_path).suffix.lower() in _IMAGE_EXT else 4
        file_info = await self._upload_file(
            file_path, file_type, openid, is_group=is_group
        )
        if file_info and await self._send_media(
            file_info, openid, is_group=is_group
        ):
            return f"✅ 文件已发送: {Path(file_path).name}"
        return f"❌ 文件发送失败: {Path(file_path).name}"

    async def send_marked_media(
        self,
        media: list[dict[str, str]],
        openid: str,
        *,
        is_group: bool = False,
    ) -> None:
        for item in media:
            if item["type"] == "image":
                result = await self.send_local_image(
                    item["path"], openid, is_group=is_group
                )
            else:
                result = await self.send_local_file(
                    item["path"], openid, is_group=is_group
                )
            self.logger.info("Codex media marker: %s", result)
