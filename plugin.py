"""会话增强工具 Pro — /fork、/rewind、/regen、/noreply、/undo 命令 + HTTP API

QwenPaw 2.x。v3.0.0 相对原版 2.4.0 的行为变化：

- 破坏性操作前自动做服务端快照（sessions/.session-tools-pro-backups/，
  每个会话保留最近 20 份），配套 /undo 与 HTTP undo 接口
- 写回前做乐观并发校验（expect_count）：操作期间 agent 若追加了消息，
  中止写入返回 409，不再静默覆盖
- fork 的 HTTP 与 slash 两条路径统一走 ChatManager 注册（删除手写
  chats.json 分支，避免与运行中的 ChatManager 互相覆盖）
- rewind/regen 一律返回该轮用户提问文本，不再把 AI 回复文本回填输入框
- delete 轮次感知：删除整个回合（用户消息 + 全部 assistant/system 链），
  而不是只删"相邻一对"
- user_id / channel 可不传：后端扫描 sessions/ 目录按文件名反查
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentscope.message import Msg, TextBlock
from fastapi import APIRouter, HTTPException, Query, Request
from qwenpaw.plugins.api import PluginApi

from .session_ops import (
    SessionChangedError,
    SessionOperator,
    sanitize_filename,
)

logger = logging.getLogger(__name__)

# 按 workspace_dir 缓存 SessionOperator
_operators: dict = {}

# HTTP 路由器
router = APIRouter()


async def _get_workspace(request: Request):
    """获取当前请求对应的 agent Workspace（多 agent 感知）。"""
    from qwenpaw.app.agent_context import get_agent_for_request

    return await get_agent_for_request(request)


async def _get_workspace_dir(request: Request) -> Path:
    """获取当前请求对应的 agent 工作区路径。"""
    ws = await _get_workspace(request)
    return Path(ws.workspace_dir).expanduser().resolve()


def _ensure_operator(workspace_dir: Path) -> SessionOperator:
    """获取或创建对应工作区的 SessionOperator。"""
    key = str(workspace_dir.resolve())
    if key not in _operators:
        _operators[key] = SessionOperator(workspace_dir)
    return _operators[key]


def _get_user_text(msg: object) -> str:
    """从消息对象中提取用户文本内容，兼容新旧两种格式。"""
    d = _msg_dict(msg)
    if d is None:
        return ""
    content = d.get("content", "")

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


def _msg_dict(msg: Any) -> dict | None:
    """提取消息记录的 dict 本体（兼容 [msg_dict, extra] 旧格式）。"""
    if isinstance(msg, list) and msg and isinstance(msg[0], dict):
        return msg[0]
    if isinstance(msg, dict):
        return msg
    return None


def _role_of(msg: Any) -> str:
    """消息角色，兼容新旧格式。"""
    d = _msg_dict(msg)
    return d.get("role", "") if d else ""


def _find_turn_user(messages: list, idx: int) -> int:
    """从 idx 往前找本轮的 user 消息下标；找不到返回 -1。"""
    for i in range(idx, -1, -1):
        if _role_of(messages[i]) == "user":
            return i
    return -1


def _find_turn_end(messages: list, start: int) -> int:
    """从 start（user 消息）往后找到下一条 user 消息为止，返回开区间右界。

    一轮 = user + 该 user 之后的全部 assistant/system（工具调用链）消息。
    """
    for j in range(start + 1, len(messages)):
        if _role_of(messages[j]) == "user":
            return j
    return len(messages)


async def _resolve_ids(
    op: SessionOperator, session_id: str, user_id: str, channel: str,
) -> tuple[str, str]:
    """user_id / channel 缺失时扫描 sessions/ 目录按文件名反查。

    文件名规则（SafeJSONSession）：sessions/{channel}/{uid}_{sid}.json
    或 sessions/{channel}/{sid}.json（uid 与 sid 相同或为空时）。
    """
    if user_id and channel:
        return user_id, channel

    base = Path(op.workspace_dir) / "sessions"
    safe_sid = sanitize_filename(session_id)
    if base.exists():
        for ch_dir in sorted(base.iterdir()):
            if not ch_dir.is_dir() or ch_dir.name == op.BACKUP_ROOT:
                continue
            ch_name = ch_dir.name
            if channel and ch_name != sanitize_filename(channel):
                continue
            for f in ch_dir.glob("*.json"):
                stem = f.stem
                if stem == safe_sid:
                    u = user_id or "default"
                elif stem.endswith("_" + safe_sid):
                    u = stem[: -len(safe_sid) - 1]
                    if user_id and u != sanitize_filename(user_id):
                        continue
                else:
                    continue
                try:
                    await op.get_messages(session_id, u, ch_name)
                    return u, ch_name
                except Exception:
                    continue

    return user_id or "default", channel or "console"


async def _locate_message(
    op: SessionOperator,
    session_id: str, user_id: str, channel: str,
    original_id: str = "", created_at: str = "",
    message_text: str = "", role: str = "",
) -> tuple[int, Any]:
    """定位消息，三层兜底：original_id 精确 → created_at 模糊 → 文本+角色。

    气泡上的 metadata.original_id 在跨设备/历史重载等场景可能缺失，
    created_at 也可能因时区/秒级误差匹配不上，因此保留文本兜底，
    避免"按钮直接罢工"。

    Returns:
        tuple[int, Any]: (文件索引, 消息对象)

    Raises:
        HTTPException: 400 无任何定位线索，404 未找到，409 多条歧义
    """
    if not original_id and not created_at and not message_text:
        raise HTTPException(
            status_code=400,
            detail="至少提供 original_id、created_at 或 message_text 参数",
        )

    file_idx: int = -1
    target_msg: Any = None

    if original_id:
        file_idx, target_msg = await op.find_message_by_id(
            session_id, user_id, channel, msg_id=original_id,
        )

    if file_idx < 0 and created_at:
        file_idx, target_msg = await op.find_message_by_created_at(
            session_id, user_id, channel, created_at=created_at,
        )

    if file_idx < 0 and message_text:
        try:
            file_idx, target_msg = await op.find_message_by_text_time_neighbors(
                session_id, user_id, channel,
                message_text=message_text, role=role,
            )
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    if file_idx < 0:
        raise HTTPException(
            status_code=404,
            detail="定位消息失败（original_id / created_at / 文本均未命中），"
                   "会话可能已被其他端修改，请刷新网页",
        )

    return file_idx, target_msg


async def _commit(
    op: SessionOperator,
    session_id: str, user_id: str, channel: str,
    before: list, after: list, op_name: str,
) -> str | None:
    """破坏性写回的统一步骤：先快照，再带并发校验写入。

    Raises:
        HTTPException: 409 会话在操作期间被修改
    """
    snapshot_id = op.snapshot(session_id, user_id, channel, op=op_name)
    try:
        await op._update_content(
            session_id, user_id, channel, after,
            expect_count=len(before),
        )
    except SessionChangedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return snapshot_id


# ── Slash 命令处理器 ────────────────────────────────────────


async def _handle_noreply_slash(ctx, args: str) -> Msg | None:
    """处理 /noreply 消息 — 发送消息但不触发 AI 回复。"""
    workspace_dir = ctx.workspace_dir
    if workspace_dir is None:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 无法获取工作区路径")])

    op = _ensure_operator(workspace_dir)
    request = getattr(ctx, "request", None)
    user_id = getattr(request, "user_id", "") or ""
    channel = getattr(request, "channel", "") or ""
    user_id, channel = await _resolve_ids(op, ctx.session_id, user_id, channel)

    text = args.strip()
    if not text:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 请输入消息内容，格式：/noreply 消息内容")])

    try:
        messages = await op.get_messages(ctx.session_id, user_id, channel)

        import uuid
        new_msg = {
            "id": uuid.uuid4().hex,
            "role": "user",
            "name": "user",
            "content": [{"type": "text", "text": text}],
            "created_at": datetime.now().isoformat(),
            "metadata": {},
        }

        if messages and isinstance(messages[0], list):
            messages.append([new_msg, {}])
        else:
            messages.append(new_msg)

        await _commit(op, ctx.session_id, user_id, channel,
                      await op.get_messages(ctx.session_id, user_id, channel),
                      messages, "noreply")

        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text",
                                      text="✅ 已记录消息（不回复），可用 /undo 撤销")])
    except HTTPException as e:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e.detail}")])
    except Exception as e:
        logger.exception("/noreply 失败")
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ 失败: {e}")])


async def _handle_rewind_slash(ctx, args: str) -> Msg | None:
    """处理 /rewind [N] — 回退 N 轮对话（快照保护，可 /undo）。"""
    workspace_dir = ctx.workspace_dir
    if workspace_dir is None:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 无法获取工作区路径")])

    op = _ensure_operator(workspace_dir)
    request = getattr(ctx, "request", None)
    user_id = getattr(request, "user_id", "") or ""
    channel = getattr(request, "channel", "") or ""
    user_id, channel = await _resolve_ids(op, ctx.session_id, user_id, channel)

    n = 1
    if args.strip():
        try:
            n = int(args.strip())
        except ValueError:
            return Msg(name="system", role="assistant",
                       content=[TextBlock(type="text", text=f"❌ 参数错误: '{args}' 不是有效的数字")])

    try:
        snapshot_id = op.snapshot(ctx.session_id, user_id, channel, op="slash-rewind")
        deleted = await op.rollback(ctx.session_id, user_id, channel, n)
        hint = "，可用 /undo 撤销" if snapshot_id else ""
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"⏪ 已回退 {n} 轮（删除了 {deleted} 条消息）{hint}")])
    except SessionChangedError as e:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e}")])
    except Exception as e:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e}")])


async def _handle_fork_slash(ctx, args: str) -> Msg | None:
    """处理 /fork [N] — 分叉新会话（统一走 ChatManager 注册）。"""
    workspace_dir = ctx.workspace_dir
    if workspace_dir is None:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 无法获取工作区路径")])

    op = _ensure_operator(workspace_dir)
    request = getattr(ctx, "request", None)
    user_id = getattr(request, "user_id", "") or ""
    channel = getattr(request, "channel", "") or ""
    user_id, channel = await _resolve_ids(op, ctx.session_id, user_id, channel)

    n = 0
    if args.strip():
        try:
            n = int(args.strip())
        except ValueError:
            return Msg(name="system", role="assistant",
                       content=[TextBlock(type="text", text=f"❌ 参数错误: '{args}' 不是有效的数字")])

    try:
        new_id = await op.fork(ctx.session_id, user_id, channel, n)

        # 获取源会话名称 + 注册到 ChatManager（唯一注册路径）
        src_name = "Chat"
        registered = False
        workspace = getattr(ctx, "workspace", None)
        cm = getattr(workspace, "chat_manager", None) if workspace else None
        if cm:
            try:
                for c in await cm.list_chats(user_id=user_id, channel=channel):
                    if c.session_id == ctx.session_id:
                        src_name = c.name or "Chat"
                        break
            except Exception:
                logger.warning("fork: 查询源会话名称失败", exc_info=True)
            try:
                from qwenpaw.app.chats.models import ChatSpec
                await cm.create_chat(ChatSpec(
                    name=f"Fork: {src_name}",
                    session_id=new_id, user_id=user_id, channel=channel,
                    created_at=datetime.now(timezone.utc),
                    meta={"forked_from": ctx.session_id, "n_rounds": n},
                ))
                registered = True
            except Exception as e:
                logger.warning("chat_manager 注册失败: %s", e)

        session_name = f"Fork: {src_name}"
        session_path = str(workspace_dir / "sessions" / channel / f"{sanitize_filename(user_id)}_{sanitize_filename(new_id)}.json")
        reg_hint = "" if registered else "（⚠️ 未注册到会话列表，需手动打开）"

        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"✅ 已分叉新会话{reg_hint}"),
                            TextBlock(type="text", text=f"📛 名称: {session_name}"),
                            TextBlock(type="text", text=f"📂 路径: {session_path}")])
    except Exception as e:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e}")])


async def _handle_regen_slash(ctx, args: str) -> Msg | None:
    """处理 /regen — 重新生成上一条回复。

    删除最后一轮（先做快照），然后修改 ctx.input_msgs[-1] 替换 /regen
    为原文本，返回 None 让 agent 继续处理。
    """
    workspace_dir = ctx.workspace_dir
    if workspace_dir is None:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 无法获取工作区路径")])

    op = _ensure_operator(workspace_dir)
    request = getattr(ctx, "request", None)
    user_id = getattr(request, "user_id", "") or ""
    channel = getattr(request, "channel", "") or ""
    user_id, channel = await _resolve_ids(op, ctx.session_id, user_id, channel)

    try:
        op.snapshot(ctx.session_id, user_id, channel, op="slash-regen")
        original_text = await op.trim_last_round(ctx.session_id, user_id, channel)
    except ValueError as e:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e}")])
    except SessionChangedError as e:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e}")])
    except Exception as e:
        logger.exception("/regen 失败")
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ 重新生成失败: {e}")])

    # 修改 ctx.input_msgs[-1]，把 /regen 替换为原用户消息
    if ctx.input_msgs:
        last_msg = ctx.input_msgs[-1]
        if hasattr(last_msg, "content"):
            content = last_msg.content
            if isinstance(content, list):
                found = False
                for block in content:
                    if hasattr(block, "type") and block.type == "text":
                        block.text = original_text
                        found = True
                        break
                if not found:
                    content.append(TextBlock(type="text", text=original_text))
            else:
                last_msg.content = [TextBlock(type="text", text=original_text)]

    return None  # 让 agent 继续处理


async def _handle_undo_slash(ctx, args: str) -> Msg | None:
    """处理 /undo — 把当前会话还原到最近一次操作前的快照。

    /undo 还原最近一份快照；/undo <snapshot_id> 还原指定快照。
    """
    workspace_dir = ctx.workspace_dir
    if workspace_dir is None:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 无法获取工作区路径")])

    op = _ensure_operator(workspace_dir)
    request = getattr(ctx, "request", None)
    user_id = getattr(request, "user_id", "") or ""
    channel = getattr(request, "channel", "") or ""
    user_id, channel = await _resolve_ids(op, ctx.session_id, user_id, channel)

    snap_id = args.strip()
    try:
        if not snap_id:
            snaps = op.list_snapshots(ctx.session_id, user_id, channel)
            if not snaps:
                return Msg(name="system", role="assistant",
                           content=[TextBlock(type="text", text="❌ 没有可还原的快照")])
            snap_id = snaps[0]
        op.restore(ctx.session_id, user_id, channel, snap_id)
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text",
                                      text=f"↩️ 已还原到快照 {snap_id}，刷新会话后生效")])
    except FileNotFoundError as e:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e}")])
    except Exception as e:
        logger.exception("/undo 失败")
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e}")])


# ── HTTP API ─────────────────────────────────────────────

@router.post("/session/{session_id}/rewind")
async def http_rewind(request: Request, session_id: str,
                      user_id: str = Query(default=""), channel: str = Query(default=""),
                      original_id: str = Query(default=""),
                      created_at: str = Query(default=""),
                      message_text: str = Query(default=""),
                      role: str = Query(default="")):
    """回退：删除定位消息及其后全部消息，返回该轮用户提问（供前端回填）。

    写入前自动快照；返回 undo_id 供 /undo 与 undo 接口使用。
    点助手气泡时不再回填 AI 文本，统一回填本轮用户提问。
    """
    try:
        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)
        user_id, channel = await _resolve_ids(op, session_id, user_id, channel)

        file_idx, _target = await _locate_message(
            op, session_id, user_id, channel,
            original_id=original_id, created_at=created_at,
            message_text=message_text, role=role,
        )

        messages = await op.get_messages(session_id, user_id, channel)
        new_messages = messages[:file_idx]
        deleted = len(messages) - len(new_messages)

        # 回填文本 = 本轮用户提问（target 是 user 时即其本身）
        uid_idx = _find_turn_user(messages, file_idx)
        rewound_message = _get_user_text(messages[uid_idx]) if uid_idx >= 0 else ""

        snapshot_id = await _commit(op, session_id, user_id, channel,
                                    messages, new_messages, "rewind")
        return {"success": True, "deleted": deleted, "remaining": len(new_messages),
                "rewound_message": rewound_message, "undo_id": snapshot_id}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except Exception as e:
        logger.exception("回退失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/session/{session_id}/regen")
async def http_regen(request: Request, session_id: str,
                     user_id: str = Query(default=""), channel: str = Query(default=""),
                     original_id: str = Query(default=""),
                     created_at: str = Query(default=""),
                     message_text: str = Query(default=""),
                     role: str = Query(default="")):
    """重新生成：回退到该助手消息所在轮的用户消息位置（删除其后全部）。

    返回用户提问文本，前端回填输入框由用户手动发送 —— 不再有
    "先删文件再 DOM 模拟发送" 的丢数据风险；失败可凭 undo_id 还原。
    """
    try:
        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)
        user_id, channel = await _resolve_ids(op, session_id, user_id, channel)

        file_idx, _target = await _locate_message(
            op, session_id, user_id, channel,
            original_id=original_id, created_at=created_at,
            message_text=message_text, role=role,
        )

        messages = await op.get_messages(session_id, user_id, channel)

        user_idx = _find_turn_user(messages, file_idx)
        if user_idx < 0:
            raise HTTPException(status_code=404, detail="重新生成失败，未找到对应的用户消息")
        user_text = _get_user_text(messages[user_idx])
        if not user_text:
            raise HTTPException(status_code=404, detail="该轮用户消息没有文本内容，无法重新生成")

        new_messages = messages[:user_idx]
        deleted = len(messages) - len(new_messages)

        snapshot_id = await _commit(op, session_id, user_id, channel,
                                    messages, new_messages, "regen")
        return {"success": True, "deleted": deleted, "remaining": len(new_messages),
                "rewound_message": user_text, "undo_id": snapshot_id}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except Exception as e:
        logger.exception("重新生成失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/session/{session_id}/fork")
async def http_fork(request: Request, session_id: str,
                    user_id: str = Query(default=""), channel: str = Query(default=""),
                    original_id: str = Query(default=""),
                    created_at: str = Query(default=""),
                    message_text: str = Query(default=""),
                    role: str = Query(default="")):
    """分叉：保留定位消息及之前全部内容生成新会话。

    注册统一走 ChatManager（与 slash /fork 同一条路径），
    不再手写 chats.json，避免与运行中的 ChatManager 互相覆盖。
    """
    try:
        ws = await _get_workspace(request)
        ws_dir = Path(ws.workspace_dir).expanduser().resolve()
        op = _ensure_operator(ws_dir)
        user_id, channel = await _resolve_ids(op, session_id, user_id, channel)

        file_idx, _target = await _locate_message(
            op, session_id, user_id, channel,
            original_id=original_id, created_at=created_at,
            message_text=message_text, role=role,
        )

        messages = await op.get_messages(session_id, user_id, channel)
        truncated = messages[:file_idx + 1]

        new_id = await op.fork(session_id, user_id, channel, 0, messages=truncated)

        # 注册到 ChatManager
        src_name = ""
        registered = False
        cm = getattr(ws, "chat_manager", None)
        if cm is not None:
            try:
                for c in await cm.list_chats(user_id=user_id, channel=channel):
                    if c.session_id == session_id:
                        src_name = c.name or ""
                        break
            except Exception:
                logger.warning("fork: 查询源会话名称失败", exc_info=True)
            try:
                from qwenpaw.app.chats.models import ChatSpec
                await cm.create_chat(ChatSpec(
                    name=f"Fork: {src_name or session_id}",
                    session_id=new_id, user_id=user_id, channel=channel,
                    created_at=datetime.now(timezone.utc),
                    meta={"forked_from": session_id},
                ))
                registered = True
            except Exception as e:
                logger.warning("chat_manager 注册失败: %s", e)
        else:
            logger.warning("fork: workspace 无 chat_manager，新会话未注册到列表")

        session_name = f"Fork: {src_name or session_id}"
        session_path = str(ws_dir / "sessions" / channel / f"{sanitize_filename(user_id)}_{sanitize_filename(new_id)}.json")

        return {"success": True, "new_session_id": new_id, "messages": len(truncated),
                "session_name": session_name, "session_path": session_path,
                "registered": registered}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except Exception as e:
        logger.exception("分叉失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/session/{session_id}/message")
async def http_delete_message(request: Request, session_id: str,
                              user_id: str = Query(default=""), channel: str = Query(default=""),
                              original_id: str = Query(default=""),
                              created_at: str = Query(default=""),
                              message_text: str = Query(default=""),
                              role: str = Query(default="")):
    """删除消息：轮次感知地删除整个回合。

    一轮 = 本轮 user 消息 + 其后全部 assistant/system（含工具调用链），
    直到下一条 user 消息为止。替代原版只删"相邻一对"的逻辑。
    """
    try:
        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)
        user_id, channel = await _resolve_ids(op, session_id, user_id, channel)

        file_idx, _target = await _locate_message(
            op, session_id, user_id, channel,
            original_id=original_id, created_at=created_at,
            message_text=message_text, role=role,
        )

        messages = await op.get_messages(session_id, user_id, channel)

        start_idx = _find_turn_user(messages, file_idx)
        if start_idx < 0:
            # 定位消息之前没有 user 消息（异常历史），退化为只删自身
            start_idx, end_idx = file_idx, file_idx + 1
        else:
            end_idx = _find_turn_end(messages, start_idx)

        new_messages = messages[:start_idx] + messages[end_idx:]
        deleted = end_idx - start_idx

        snapshot_id = await _commit(op, session_id, user_id, channel,
                                    messages, new_messages, "delete")
        return {"success": True, "deleted": deleted, "remaining": len(new_messages),
                "undo_id": snapshot_id}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except Exception as e:
        logger.exception("删除失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/session/{session_id}/undo")
async def http_undo(request: Request, session_id: str,
                    user_id: str = Query(default=""), channel: str = Query(default=""),
                    snapshot_id: str = Query(default="")):
    """还原会话到指定快照（缺省 = 最近一份）。"""
    try:
        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)
        user_id, channel = await _resolve_ids(op, session_id, user_id, channel)

        snap_id = snapshot_id.strip()
        if not snap_id:
            snaps = op.list_snapshots(session_id, user_id, channel)
            if not snaps:
                raise HTTPException(status_code=404, detail="没有可还原的快照")
            snap_id = snaps[0]

        op.restore(session_id, user_id, channel, snap_id)
        return {"success": True, "restored": snap_id}
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("还原失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}/snapshots")
async def http_list_snapshots(request: Request, session_id: str,
                              user_id: str = Query(default=""), channel: str = Query(default="")):
    """列出会话快照 ID（新的在前）。"""
    try:
        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)
        user_id, channel = await _resolve_ids(op, session_id, user_id, channel)
        return {"success": True, "snapshots": op.list_snapshots(session_id, user_id, channel)}
    except Exception as e:
        logger.exception("列出快照失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}/message")
async def http_get_message(request: Request, session_id: str,
                           user_id: str = Query(default=""), channel: str = Query(default=""),
                           original_id: str = Query(default=""),
                           created_at: str = Query(default=""),
                           message_text: str = Query(default=""),
                           role: str = Query(default="")):
    """获取消息详情：按 original_id 或 created_at 查找消息，返回完整 JSON。"""
    try:
        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)
        user_id, channel = await _resolve_ids(op, session_id, user_id, channel)

        file_idx, target_msg = await _locate_message(
            op, session_id, user_id, channel,
            original_id=original_id, created_at=created_at,
            message_text=message_text, role=role,
        )

        msg_dict = _msg_dict(target_msg)

        return {"success": True, "index": file_idx, "message": msg_dict}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except Exception as e:
        logger.exception("获取消息失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}/info")
async def http_session_info(request: Request, session_id: str,
                            user_id: str = Query(default=""),
                            channel: str = Query(default="")):
    """获取会话信息。"""
    try:
        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)
        user_id, channel = await _resolve_ids(op, session_id, user_id, channel)
        messages = await op.get_messages(session_id, user_id, channel)

        role_count: dict = {}
        for m in messages:
            role = _role_of(m)
            if role:
                role_count[role] = role_count.get(role, 0) + 1

        return {"session_id": session_id, "total_messages": len(messages), "role_counts": role_count}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 插件入口 ──────────────────────────────────────────────


class Plugin:
    """会话增强工具 Pro 插件主类。"""

    def register(self, api: PluginApi):
        """注册插件到 QwenPaw 系统。"""
        logger.info("注册 SessionToolsPro 插件...")

        api.register_slash_command(
            name="rewind", handler=_handle_rewind_slash,
            category="plugin", help_text="回退 N 轮对话，例如 /rewind 3",
        )
        api.register_slash_command(
            name="fork", handler=_handle_fork_slash,
            category="plugin", help_text="分叉新会话，例如 /fork 2",
        )
        api.register_slash_command(
            name="regen", handler=_handle_regen_slash,
            category="plugin", help_text="重新生成上一条回复",
        )
        api.register_slash_command(
            name="noreply", handler=_handle_noreply_slash,
            category="plugin", help_text="发送消息但不触发 AI 回复，例如 /noreply 好的",
        )
        api.register_slash_command(
            name="undo", handler=_handle_undo_slash,
            category="plugin", help_text="还原到上次操作前的快照，例如 /undo",
        )

        api.register_http_router(router, prefix="/session-tools", tags=["session-tools"])

        logger.info("✓ SessionToolsPro 插件注册完成")


plugin = Plugin()
