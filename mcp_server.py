#!/usr/bin/env python3
"""
ClawBridge MCP Server (v0.5.0)
标准 MCP (Model Context Protocol) 服务器入口

协议规范：
  - stdin/stdout：专属 MCP JSON-RPC 2.0 通信，禁止任何其他输出
  - stderr：所有日志、调试信息
"""

import asyncio
import json
import sys
import os
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
os.chdir(SCRIPT_DIR)
sys.path.insert(0, str(SCRIPT_DIR))

import yaml
from clawbridge_client import ClawBridgeClient, CLAWBRIDGE_DIR

DEBUG_LOG = CLAWBRIDGE_DIR / "debug.log"


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


def dlog(msg: str):
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def send_response(obj: dict):
    data = (json.dumps(obj) + "\n").encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


TOOLS = [
    {
        "name": "send_clawbridge_message",
        "description": (
            "通过 ClawBridge 加密网络向另一个 AI Agent 发送端到端加密消息。"
            "target 可以是对方的手机号（如 @10001）或电话本中保存的 Agent 名称。"
            "对方不在线时消息会由服务器暂存，上线后自动投递。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "目标 Agent 的手机号（如 @10001）或电话本中的名称"
                },
                "content": {
                    "type": "string",
                    "description": "要发送的消息内容（明文，发送前自动加密）"
                }
            },
            "required": ["target", "content"]
        }
    },
    {
        "name": "check_messages",
        "description": (
            "查收其他 Agent 通过 ClawBridge 发来的新消息。"
            "在以下情况下调用：用户询问是否有新消息、用户要求查收消息、"
            "或刚发送消息后等待对方回复时。"
            "调用后收件箱自动清空。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "connection_status",
        "description": "查询 ClawBridge 当前连接状态。返回本机手机号、连接状态、版本号。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "list_contacts",
        "description": "查看电话本中保存的所有 Agent 联系人（手机号 + 备注名）。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "save_contact",
        "description": "在电话本中保存或更新一个联系人的备注名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "对方的手机号，如 @10002"
                },
                "agent_name": {
                    "type": "string",
                    "description": "给这个联系人起的备注名，如「Alice / Aily」"
                }
            },
            "required": ["id", "agent_name"]
        }
    }
]


class ClawBridgeMCPServer:

    def __init__(self):
        config_path = SCRIPT_DIR / "config.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        self.client = ClawBridgeClient(config["broker_url"])
        self.client.set_callback(self._on_message)

    def _on_message(self, sender_id: str, clear_text: str, msg_id: str = "", timestamp: int = 0):
        # 消息持久化已由 ClawBridgeClient._inbox_persist() 处理，此处仅记录日志
        log(f"[ClawBridge] 📨 收到来自 {sender_id} 的消息: {clear_text}")
        dlog(f"_on_message: from={sender_id} msg_id={msg_id} text={clear_text!r}")

    async def _handle_tool_call(self, name: str, arguments: dict) -> str:
        if name == "send_clawbridge_message":
            target  = arguments.get("target", "").strip()
            content = arguments.get("content", "").strip()
            if not target or not content:
                return "参数缺失：target 和 content 均为必填项。"
            try:
                await self.client.send_message(target, content)
                await asyncio.sleep(1.0)
                # 解析实际发送的 ID 用于显示
                resolved = self.client._resolve_target(target)
                name_display = self.client._get_agent_name(resolved)
                display = f"{name_display}（{resolved}）" if name_display != resolved else resolved
                return f"消息已成功加密发送至 {display}。对方不在线时服务器将暂存并在其上线后投递。"
            except Exception as e:
                return f"发送失败：{str(e)}"

        if name == "check_messages":
            await asyncio.sleep(2.0)
            messages = self.client.drain_inbox()
            if not messages:
                return "📭 暂无新消息。"
            lines = [f"📨 收到 {len(messages)} 条新消息：\n"]
            for m in messages:
                sender_id  = m["from"]
                agent_name = self.client._get_agent_name(sender_id)
                display    = f"{agent_name}（{sender_id}）" if agent_name != sender_id else sender_id
                lines.append(f"【来自 {display}】{m['text']}")
            return "\n".join(lines)

        if name == "connection_status":
            registered = self.client._registered
            client_id  = self.client.client_id or "（未注册）"
            ws_state   = "已连接" if self.client.ws is not None else "未连接"
            status     = "✅ 已注册入网" if registered else "⏳ 连接中（握手未完成）"
            result = (
                f"ClawBridge MCP Server v0.5.0\n"
                f"手机号：{client_id}\n"
                f"WebSocket：{ws_state}\n"
                f"注册状态：{status}\n"
                f"数据目录：{CLAWBRIDGE_DIR}"
            )
            if self.client._notice:
                result = self.client._notice + "\n\n" + result
                self.client._notice = ""
            return result

        if name == "list_contacts":
            contacts = self.client.get_contacts()
            if not contacts:
                return "📒 电话本为空。收到或发送消息后会自动添加联系人，也可使用 save_contact 手动添加。"
            lines = [f"📒 电话本（共 {len(contacts)} 位联系人）：\n"]
            for c in contacts:
                name_part = f"「{c['agent_name']}」" if c.get("agent_name") else "（未备注）"
                lines.append(f"  {c['id']}  {name_part}  加入于 {c.get('added_at','')}")
            return "\n".join(lines)

        if name == "save_contact":
            agent_id   = arguments.get("id", "").strip()
            agent_name = arguments.get("agent_name", "").strip()
            if not agent_id or not agent_name:
                return "参数缺失：id 和 agent_name 均为必填项。"
            result = self.client.save_contact(agent_id, agent_name)
            return result

        return f"未知工具：{name}"

    async def _handle_request(self, req: dict):
        method = req.get("method", "")
        req_id = req.get("id")

        if method == "initialize":
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "clawbridge", "version": "0.5.0"}
                }
            })

        elif method == "notifications/initialized":
            pass

        elif method == "tools/list":
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS}
            })

        elif method == "tools/call":
            params    = req.get("params", {})
            name      = params.get("name", "")
            arguments = params.get("arguments", {})
            result_text = await self._handle_tool_call(name, arguments)
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}]
                }
            })

        else:
            if req_id is not None:
                send_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}
                })

    async def run(self):
        asyncio.create_task(self.client.connect_and_listen())
        log("[ClawBridge MCP] 服务器已启动，等待宿主指令...")

        loop  = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _stdin_reader():
            try:
                while True:
                    raw = sys.stdin.buffer.readline()
                    if not raw:
                        break
                    stripped = raw.decode("utf-8", errors="replace").strip()
                    loop.call_soon_threadsafe(queue.put_nowait, stripped)
            except Exception as e:
                log(f"[ClawBridge MCP] stdin 读取异常: {e}")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_stdin_reader, daemon=True).start()

        while True:
            line = await queue.get()
            if line is None:
                log("[ClawBridge MCP] stdin 已关闭，退出")
                break
            if not line:
                continue
            try:
                request = json.loads(line)
                await self._handle_request(request)
            except json.JSONDecodeError as e:
                log(f"[ClawBridge MCP] JSON 解析失败: {e}")
            except Exception as e:
                log(f"[ClawBridge MCP] 处理请求异常: {e}")


if __name__ == "__main__":
    try:
        server = ClawBridgeMCPServer()
        asyncio.run(server.run())
    except Exception as e:
        log(f"[ClawBridge MCP] 启动失败: {e}")
        sys.exit(1)
