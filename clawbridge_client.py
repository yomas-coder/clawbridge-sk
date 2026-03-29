import asyncio
import json
import base64
import sys
import time
from pathlib import Path

import websockets
from nacl.public import PrivateKey, PublicKey, Box
from nacl.encoding import Base64Encoder
import nacl.utils


def _log(msg: str):
    """统一日志输出：写 stderr，兼容 MCP 模式（stdout 专属协议通道）"""
    print(msg, file=sys.stderr, flush=True)


CLAWBRIDGE_DIR = Path.home() / ".clawbridge"
IDENTITY_FILE  = CLAWBRIDGE_DIR / "identity.json"
CONTACTS_FILE  = CLAWBRIDGE_DIR / "contacts.json"


class ClawBridgeClient:
    """
    ClawBridge 统一客户端 v0.5.0
    - 首次启动自动注册，获取数字 ID（@10001）和 API Key
    - 身份持久化在 ~/.clawbridge/identity.json
    - 电话本持久化在 ~/.clawbridge/contacts.json
    """

    def __init__(self, broker_url: str):
        self.broker_url = broker_url
        self.ws = None
        self._registered = False

        # ── 加载或初始化身份 ──────────────────────────────────────────
        CLAWBRIDGE_DIR.mkdir(exist_ok=True)
        identity = self._load_identity()
        if identity:
            self.client_id = identity["id"]
            self.api_key   = identity["api_key"]
            self.private_key = self._load_or_generate_key(self.client_id)
            _log(f"[ClawBridge] 🔑 身份已加载：{self.client_id}")
        else:
            # 未注册：生成临时密钥，等待连接后完成注册
            self.client_id = None
            self.api_key   = None
            self.private_key = PrivateKey.generate()
            _log("[ClawBridge] 🆕 未找到身份文件，将在连接后自动注册")

        self.public_key_b64 = self.private_key.public_key.encode(
            encoder=Base64Encoder
        ).decode("utf-8")

        self.peer_keys_cache  = {}   # { "@10002": "base64_pub_key" or None }
        self.message_callback = None
        self._pending_messages = []
        self.lookup_events = {}
        self._notice = ""              # 待推送给 connection_status 的通知（读后清空）
        self._pending_reregister = False  # 因账号被删触发的重注册标记

    # ── 身份持久化 ────────────────────────────────────────────────────

    def _load_identity(self) -> dict | None:
        try:
            if IDENTITY_FILE.exists():
                data = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))
                if data.get("id") and data.get("api_key"):
                    return data
        except Exception:
            pass
        return None

    def _save_identity(self):
        try:
            IDENTITY_FILE.write_text(
                json.dumps({"id": self.client_id, "api_key": self.api_key},
                           ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            _log(f"[ClawBridge] 💾 身份已保存：{IDENTITY_FILE}")
        except Exception as e:
            _log(f"[ClawBridge] ⚠️ 身份文件写入失败（内存中仍有效）: {e}")

    def _load_or_generate_key(self, client_id: str) -> PrivateKey:
        safe_id = client_id.lstrip("@")
        key_file = CLAWBRIDGE_DIR / f"{safe_id}.key"
        if key_file.exists():
            key_b64 = key_file.read_text().strip()
            _log(f"[ClawBridge] 🔑 已加载 E2E 密钥：{key_file}")
            return PrivateKey(key_b64.encode("utf-8"), encoder=Base64Encoder)
        else:
            private_key = PrivateKey.generate()
            key_b64 = private_key.encode(encoder=Base64Encoder).decode("utf-8")
            try:
                key_file.write_text(key_b64)
                _log(f"[ClawBridge] 🔑 新 E2E 密钥已生成并保存：{key_file}")
            except Exception as e:
                _log(f"[ClawBridge] ⚠️ E2E 密钥文件写入失败（内存中仍有效）: {e}")
            return private_key

    def _save_key_file(self, client_id: str):
        """注册成功后将内存中的临时密钥保存到正式文件"""
        safe_id = client_id.lstrip("@")
        key_file = CLAWBRIDGE_DIR / f"{safe_id}.key"
        key_b64 = self.private_key.encode(encoder=Base64Encoder).decode("utf-8")
        try:
            key_file.write_text(key_b64)
            _log(f"[ClawBridge] 🔑 E2E 密钥已保存：{key_file}")
        except Exception as e:
            _log(f"[ClawBridge] ⚠️ E2E 密钥文件写入失败: {e}")

    def _clear_identity(self):
        """清除本地身份（identity.json + key 文件），触发下次重连时重新注册"""
        # 清除 identity.json
        try:
            if IDENTITY_FILE.exists():
                IDENTITY_FILE.unlink()
                _log(f"[ClawBridge] 🗑️ identity.json 已清除")
        except Exception as e:
            _log(f"[ClawBridge] ⚠️ 清除 identity.json 失败: {e}")
        # 清除旧 key 文件
        if self.client_id:
            safe_id = self.client_id.lstrip("@")
            key_file = CLAWBRIDGE_DIR / f"{safe_id}.key"
            try:
                if key_file.exists():
                    key_file.unlink()
                    _log(f"[ClawBridge] 🗑️ 密钥文件已清除: {key_file}")
            except Exception as e:
                _log(f"[ClawBridge] ⚠️ 清除密钥文件失败: {e}")
        # 重置内存中的身份，生成新密钥对
        self.client_id = None
        self.api_key = None
        self._pending_reregister = True
        self.private_key = PrivateKey.generate()
        self.public_key_b64 = self.private_key.public_key.encode(
            encoder=Base64Encoder
        ).decode("utf-8")

    # ── 电话本 ────────────────────────────────────────────────────────

    def _load_contacts(self) -> list:
        try:
            if CONTACTS_FILE.exists():
                data = json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save_contacts(self, contacts: list):
        try:
            CONTACTS_FILE.write_text(
                json.dumps(contacts, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            _log(f"[ClawBridge] ⚠️ 电话本写入失败: {e}")

    def get_contacts(self) -> list:
        return self._load_contacts()

    def save_contact(self, agent_id: str, agent_name: str) -> str:
        """新增或更新联系人备注名，返回操作结果说明"""
        agent_id = agent_id.lower().strip()
        contacts = self._load_contacts()
        for c in contacts:
            if c["id"] == agent_id:
                old_name = c.get("agent_name", "")
                c["agent_name"] = agent_name
                self._save_contacts(contacts)
                return f"已更新：{agent_id} 的备注从「{old_name}」改为「{agent_name}」"
        contacts.append({
            "id": agent_id,
            "agent_name": agent_name,
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        self._save_contacts(contacts)
        return f"已添加联系人：{agent_name}（{agent_id}）"

    def _auto_add_contact(self, agent_id: str):
        """收发消息时自动将陌生 ID 加入电话本（agent_name 留空）"""
        contacts = self._load_contacts()
        if any(c["id"] == agent_id for c in contacts):
            return
        contacts.append({
            "id": agent_id,
            "agent_name": "",
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        self._save_contacts(contacts)
        _log(f"[ClawBridge] 📒 新联系人已加入电话本：{agent_id}")

    def _get_agent_name(self, agent_id: str) -> str:
        """从电话本查找备注名，没有则返回 ID 本身"""
        for c in self._load_contacts():
            if c["id"] == agent_id and c.get("agent_name"):
                return c["agent_name"]
        return agent_id

    def _resolve_target(self, target: str) -> str:
        """将 agent_name 或 ID 解析为 @ID 格式"""
        target = target.strip()
        if target.startswith("@"):
            return target.lower()
        # 尝试按名称查找
        for c in self._load_contacts():
            if c.get("agent_name", "").lower() == target.lower():
                return c["id"]
        # 当作 ID 使用
        return ("@" + target).lower()

    # ── 回调注册 ──────────────────────────────────────────────────────

    def on_message(self, callback):
        self.message_callback = callback
        self._flush_pending_messages()

    def set_callback(self, callback):
        self.message_callback = callback
        self._flush_pending_messages()

    def _flush_pending_messages(self):
        if self.message_callback and self._pending_messages:
            pending = list(self._pending_messages)
            self._pending_messages.clear()
            for msg in pending:
                self.message_callback(*msg)

    # ── 连接方式 ──────────────────────────────────────────────────────

    async def connect_and_listen(self):
        """后台长驻任务：连接、注册（首次）、握手、持续监听，自动重连"""
        try:
            self.ws = await self._connect_ws()

            # 首次运行或账号被删后：注册获取 ID + API Key
            if self.client_id is None:
                await self._register()
                if self._pending_reregister:
                    self._notice = (
                        f"⚠️ 原账号已被管理员删除，已自动重新注册，"
                        f"新手机号：{self.client_id}。请将新手机号告知联系人。"
                    )
                    _log(f"[ClawBridge] 📱 {self._notice}")
                    self._pending_reregister = False

            await self._send_handshake()

            # 等待握手 ack
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=15.0)
                data = json.loads(raw)
                payload = data.get("payload", {})
                if data.get("type") == "ack" and payload.get("status") == "error":
                    msg = payload.get("message", "")
                    if msg == "user_not_found":
                        _log("[ClawBridge] 🗑️ 账号已被删除，清除本地身份，将自动重新注册...")
                        self._clear_identity()
                    elif msg == "invalid_key":
                        self._notice = (
                            "⚠️ API Key 已被管理员重置，请从管理员处获取新 Key，"
                            "手动更新 ~/.clawbridge/identity.json 中的 api_key 字段，然后重启 skill。"
                        )
                        _log(f"[ClawBridge] ⚠️ {self._notice}")
                    raise Exception(f"握手失败: {msg}")
            except asyncio.TimeoutError:
                raise Exception("握手超时，broker 未响应")

            self._registered = True
            _log(f"[ClawBridge] 📶 {self.client_id} 成功入网！")

            async for message in self.ws:
                asyncio.create_task(self._route_message(json.loads(message)))

        except Exception as e:
            _log(f"[ClawBridge] ❌ 失去基站信号: {e}，5 秒后重连...")
        self.ws = None
        self._registered = False
        await asyncio.sleep(5)
        asyncio.create_task(self.connect_and_listen())

    async def _connect_ws(self):
        try:
            return await websockets.connect(
                self.broker_url,
                ping_interval=20,
                ping_timeout=10,
            )
        except TypeError:
            return await websockets.connect(self.broker_url)

    # ── 注册流程 ──────────────────────────────────────────────────────

    async def _register(self):
        """向 broker 发送 register 请求，同步等待 ack，保存身份"""
        reg_msg = {
            "msg_id": f"reg-{int(time.time() * 1000)}",
            "type": "register",
            "from": "",
            "to": "broker",
            "timestamp": int(time.time() * 1000),
            "payload": {
                "public_key": self.public_key_b64,
                "client_version": "v0.5",
            },
        }
        await self.ws.send(json.dumps(reg_msg))
        _log("[ClawBridge] 📤 注册请求已发送，等待 broker 响应...")

        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=15.0)
        except asyncio.TimeoutError:
            raise Exception("注册超时，broker 未响应")

        data = json.loads(raw)
        payload = data.get("payload", {})
        if data.get("type") != "ack" or payload.get("status") != "success":
            raise Exception(f"注册失败：{payload.get('message', '未知错误')}")

        self.client_id = payload["id"]
        self.api_key   = payload["api_key"]
        _log(f"[ClawBridge] 🎉 注册成功！分配 ID：{self.client_id}")

        self._save_key_file(self.client_id)
        self._save_identity()

    # ── 握手 ──────────────────────────────────────────────────────────

    async def _send_handshake(self):
        handshake = {
            "msg_id": f"req-{int(time.time() * 1000)}",
            "type": "handshake",
            "from": self.client_id,
            "to": "broker",
            "timestamp": int(time.time() * 1000),
            "payload": {
                "api_key": self.api_key,
                "public_key": self.public_key_b64,
                "client_version": "v0.5",
            },
        }
        await self.ws.send(json.dumps(handshake))

    # ── 消息路由 ──────────────────────────────────────────────────────

    async def _route_message(self, data: dict):
        msg_type = data.get("type")
        _log(f"[{self.client_id}] _route_message: type={msg_type}")

        if msg_type == "ack" and data.get("payload", {}).get("target_id"):
            target_id = data["payload"]["target_id"]
            pub_key   = data["payload"].get("public_key")
            _log(f"[ClawBridge] 收到 lookup ack: target={target_id}, has_key={bool(pub_key)}")
            if pub_key:
                self.peer_keys_cache[target_id] = pub_key
            else:
                self.peer_keys_cache[target_id] = None  # 已确认：用户未注册
            if target_id in self.lookup_events:
                self.lookup_events[target_id].set()

        elif msg_type == "relay":
            sender_id = data.get("from")
            _log(f"[{self.client_id}] relay from {sender_id}, decrypting...")
            self._auto_add_contact(sender_id)
            await self._decrypt_and_callback(sender_id, data["payload"])

    # ── 解密 ──────────────────────────────────────────────────────────

    async def _decrypt_and_callback(self, sender_id: str, encrypted_payload: dict):
        if sender_id not in self.peer_keys_cache:
            await self._lookup_peer(sender_id)

        sender_pub_key_b64 = self.peer_keys_cache.get(sender_id)
        if not sender_pub_key_b64:
            _log(f"[{self.client_id}] ❌ 无法获取 {sender_id} 的公钥，解密失败")
            return

        try:
            sender_pub_key = PublicKey(
                sender_pub_key_b64.encode("utf-8"), encoder=Base64Encoder
            )
            box = Box(self.private_key, sender_pub_key)
            nonce      = base64.b64decode(encrypted_payload["nonce"])
            ciphertext = base64.b64decode(encrypted_payload["ciphertext"])
            decrypted_json = box.decrypt(ciphertext, nonce).decode("utf-8")

            try:
                envelope = json.loads(decrypted_json)
                msg_id    = envelope.get("msg_id", "")
                timestamp = envelope.get("timestamp", 0)
                text      = envelope.get("text", decrypted_json)
            except json.JSONDecodeError:
                msg_id = ""; timestamp = 0; text = decrypted_json

            if self.message_callback:
                self.message_callback(sender_id, text, msg_id, timestamp)
            else:
                self._pending_messages.append((sender_id, text, msg_id, timestamp))
                _log(f"[{self.client_id}] ⚠️ 回调未注册，消息已缓存")
        except Exception as e:
            _log(f"[{self.client_id}] ❌ 解密异常: {e}")

    # ── 寻址 ──────────────────────────────────────────────────────────

    async def _lookup_peer(self, target_id: str):
        lookup_msg = {
            "msg_id": f"lookup-{int(time.time() * 1000)}",
            "type": "lookup",
            "from": self.client_id,
            "to": "broker",
            "timestamp": int(time.time() * 1000),
            "payload": {"target_id": target_id},
        }
        event = asyncio.Event()
        self.lookup_events[target_id] = event
        await self.ws.send(json.dumps(lookup_msg))
        try:
            await asyncio.wait_for(event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            _log(f"[{self.client_id}] ⚠️ 查询 {target_id} 超时")
        finally:
            self.lookup_events.pop(target_id, None)

    # ── 发送消息 ──────────────────────────────────────────────────────

    async def send_message(self, target: str, plain_text: str):
        """
        加密并发送消息。target 可以是 @ID 或电话本中的 agent_name。
        目标离线时 broker 自动入队，发送方视为成功。
        """
        target_id = self._resolve_target(target)

        # 等待握手完成（最多 5 秒）
        for _ in range(50):
            if self._registered:
                break
            await asyncio.sleep(0.1)
        if not self._registered:
            raise Exception("未能连接到 ClawBridge 基站，请稍后重试。")

        if target_id not in self.peer_keys_cache:
            _log(f"[{self.client_id}] 正在寻址 {target_id} 的公钥...")
            await self._lookup_peer(target_id)

        if target_id not in self.peer_keys_cache:
            raise Exception(f"连接超时，无法找到用户 {target_id}，请检查网络或稍后重试。")
        target_pub_key_b64 = self.peer_keys_cache[target_id]
        if target_pub_key_b64 is None:
            raise Exception(f"用户 {target_id} 不存在（未注册），请确认 ID 是否正确。")

        msg_id    = f"msg-{int(time.time() * 1000)}"
        timestamp = int(time.time() * 1000)
        message_envelope = json.dumps(
            {"msg_id": msg_id, "timestamp": timestamp, "text": plain_text},
            ensure_ascii=False
        )

        target_pub_key = PublicKey(
            target_pub_key_b64.encode("utf-8"), encoder=Base64Encoder
        )
        box   = Box(self.private_key, target_pub_key)
        nonce = nacl.utils.random(Box.NONCE_SIZE)
        encrypted_bytes  = box.encrypt(message_envelope.encode("utf-8"), nonce)
        ciphertext_only  = encrypted_bytes[len(nonce):]

        relay_msg = {
            "msg_id": msg_id,
            "type": "relay",
            "from": self.client_id,
            "to": target_id,
            "timestamp": timestamp,
            "payload": {
                "nonce":      base64.b64encode(nonce).decode("utf-8"),
                "ciphertext": base64.b64encode(ciphertext_only).decode("utf-8"),
            },
        }
        await self.ws.send(json.dumps(relay_msg))
        self._auto_add_contact(target_id)
        _log(f"[{self.client_id}] 🚀 已向 {target_id} 发送加密消息")
