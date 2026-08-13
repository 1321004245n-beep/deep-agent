#!/usr/bin/env python3
"""Web 对话服务 —— 为 Deep Agent 提供 HTTP + SSE 流式聊天接口。

启动:
    python web_server.py                # 默认 0.0.0.0:8000
    python web_server.py --port 9000

接口:
    GET  /            返回 Web Chat UI 页面
    GET  /static/*    静态资源 (css/js)
    POST /api/chat    SSE 流式对话
        body: {"message": "...", "thread_id": "会话id(可选)", "history": [...]}
        - 传 thread_id 时: 服务端通过 checkpointer 按会话恢复完整状态（推荐）
        - 不传 thread_id: 回退为前端回传 history 的无状态模式（兼容旧客户端）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from pydantic import BaseModel

from agent_service import (
    CHECKPOINT_DB_PATH,
    build_agent,
    create_async_checkpointer,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
SESSIONS_DB_PATH = os.path.join(BASE_DIR, ".agents", "sessions.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
MAX_HISTORY_ROUNDS = 10  # 无 thread_id 时回退用的历史轮数上限
MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 附件大小上限 5MB

# 可用模型注册表（首项为默认）
MODEL_REGISTRY = ["deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner", "glm-4-flash"]
DEFAULT_MODEL = MODEL_REGISTRY[0]

# agent 池: 按模型名缓存实例（共享 checkpointer/store/backend 连接）
agents: dict[str, Any] = {}
_checkpointer = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期: 预构建默认模型 agent, 其余按需构建; 关闭时释放连接。"""
    global _checkpointer
    _checkpointer = await create_async_checkpointer()
    agents[DEFAULT_MODEL] = build_agent(model=DEFAULT_MODEL, checkpointer=_checkpointer)
    yield
    await _checkpointer.conn.close()


def get_agent(model: str) -> Any:
    """按模型名取 agent（懒构建 + 缓存, 共享持久化连接）。"""
    model = (model or DEFAULT_MODEL).strip()
    if model not in MODEL_REGISTRY:
        model = DEFAULT_MODEL
    if model not in agents:
        agents[model] = build_agent(model=model, checkpointer=_checkpointer)
    return agents[model]


app = FastAPI(title="Deep Agent Web Chat", docs_url=None, redoc_url=None, lifespan=lifespan)


# ---------------------------------------------------------------------------
# 会话元数据服务端持久化 (sessions.db)
#   服务端保存会话列表 + 消息快照, 前端 localStorage 仅作渲染缓存
# ---------------------------------------------------------------------------
def _sessions_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(SESSIONS_DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '新会话',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            messages TEXT NOT NULL DEFAULT '[]',
            deleted_at INTEGER NOT NULL DEFAULT 0
        )"""
    )
    # 老库迁移: 补 deleted_at 列（软删除标记, 0=未删除）
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN deleted_at INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 列已存在
    return conn


def _row_to_session(row: tuple) -> dict[str, Any]:
    return {
        "id": row[0],
        "thread_id": row[1],
        "title": row[2],
        "created_at": row[3],
        "updated_at": row[4],
        "messages": json.loads(row[5] or "[]"),
        "deleted_at": row[6],
    }


class SessionSync(BaseModel):
    id: str
    thread_id: str = ""
    title: str = "新会话"
    created_at: int
    updated_at: int
    messages: list[dict[str, Any]] = []


@app.get("/api/sessions")
def list_sessions() -> list[dict[str, Any]]:
    """会话列表（不含消息体, 按最近更新倒序, 排除已删除）。"""
    conn = _sessions_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE deleted_at=0 ORDER BY updated_at DESC"
        ).fetchall()
        return [{k: v for k, v in _row_to_session(r).items() if k not in ("messages", "deleted_at")} for r in rows]
    finally:
        conn.close()


@app.get("/api/sessions/deleted")
def list_deleted_sessions() -> list[dict[str, Any]]:
    """已删除会话的 id 列表（tombstone, 供其他客户端清理本地缓存, 防止复活）。

    注意: 必须注册在 /api/sessions/{session_id} 之前, 避免路由冲突。
    """
    conn = _sessions_conn()
    try:
        rows = conn.execute(
            "SELECT id, deleted_at FROM sessions WHERE deleted_at>0 ORDER BY deleted_at DESC"
        ).fetchall()
        return [{"id": r[0], "deleted_at": r[1]} for r in rows]
    finally:
        conn.close()


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    """会话详情（含消息快照, 供跨设备恢复渲染）。"""
    conn = _sessions_conn()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return {"error": "not found"}
        data = _row_to_session(row)
        if data["deleted_at"]:
            return {"error": "deleted"}
        return data
    finally:
        conn.close()


@app.post("/api/sessions/sync")
def sync_session(req: SessionSync) -> dict[str, Any]:
    """创建/更新会话（upsert 元数据 + 消息快照）。

    防复活: 若该会话已被软删除, 且本次同步的 updated_at 早于删除时间
    （说明是旧客户端缓存的过时数据）→ 拒绝; 若晚于删除时间（删除后
    确实产生了新活动）→ 接受并恢复。
    """
    conn = _sessions_conn()
    try:
        row = conn.execute("SELECT deleted_at FROM sessions WHERE id=?", (req.id,)).fetchone()
        if row and row[0] > 0 and req.updated_at < row[0]:
            return {"ok": False, "reason": "deleted"}
        conn.execute(
            """INSERT INTO sessions (id, thread_id, title, created_at, updated_at, messages, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(id) DO UPDATE SET
                 thread_id=excluded.thread_id,
                 title=excluded.title,
                 created_at=excluded.created_at,
                 updated_at=excluded.updated_at,
                 messages=excluded.messages,
                 deleted_at=0""",
            (
                req.id,
                req.thread_id,
                req.title,
                req.created_at,
                req.updated_at,
                json.dumps(req.messages, ensure_ascii=False),
            ),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, Any]:
    """软删除会话（tombstone 标记）, 并尽力清理 checkpointer 中对应 thread。

    软删除而非物理删除: 其他客户端 bootstrap 时通过 /api/sessions/deleted
    发现删除操作, 同步清理本地缓存, 避免"复活"。
    """
    conn = _sessions_conn()
    try:
        row = conn.execute("SELECT thread_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        conn.execute(
            "UPDATE sessions SET deleted_at=? WHERE id=? AND deleted_at=0",
            (int(time.time() * 1000), session_id),
        )
        conn.commit()
        if row:
            _purge_thread(row[0])
        return {"ok": True}
    finally:
        conn.close()


def _purge_thread(thread_id: str) -> None:
    """从 checkpoints.db 清理指定 thread（忽略失败, 不影响会话删除）。"""
    try:
        conn = sqlite3.connect(CHECKPOINT_DB_PATH)
        try:
            conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))
            conn.execute("DELETE FROM writes WHERE thread_id=?", (thread_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


class ChatRequest(BaseModel):
    message: str
    thread_id: str = ""  # 会话 id（服务端 checkpointer 恢复维度）
    model: str = ""  # 模型名, 空则用默认 (deepseek-v4-flash)
    history: list[dict[str, str]] = []  # 兼容旧客户端: [{"role", "content"}]
    attachments: list[dict[str, Any]] = []  # 附件: [{"name", "path", "size"}]


def _attach_desc(req: ChatRequest) -> str:
    """把附件信息拼成给 Agent 的提示文本。"""
    if not req.attachments:
        return ""
    lines = ["", "[用户上传了以下附件, 可用 read_file 工具读取内容后分析:]"]
    for a in req.attachments:
        base = f"- {a.get('name', '')} (路径: {a.get('path', '')}, {a.get('size', 0)} 字节"
        if a.get("parsed_path"):
            base += f", 已解析为纯文本: {a.get('parsed_path')} —— 直接读取该解析文件最方便)"
        else:
            base += ")"
        lines.append(base)
    return "\n".join(lines)


def _build_messages(req: ChatRequest) -> list[Any]:
    """回退模式: 把前端 history + 新消息转成 LangChain 消息列表。"""
    messages: list[Any] = []
    for m in req.history[-MAX_HISTORY_ROUNDS * 2:]:
        if m.get("role") == "user":
            messages.append(HumanMessage(content=m.get("content", "")))
        elif m.get("role") == "assistant":
            messages.append(AIMessage(content=m.get("content", "")))
    messages.append(HumanMessage(content=req.message))
    return messages


def _chunk_text(chunk: Any) -> str:
    """从 AIMessageChunk 中提取纯文本（兼容 str 与多模态 list 两种 content）。"""
    if isinstance(chunk, AIMessageChunk):
        content = chunk.content
    else:
        content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def _truncate_text(text: Any, limit: int = 800) -> str:
    """工具输出截断, 防止超大内容撑爆前端。"""
    if isinstance(text, (dict, list)):
        text = json.dumps(text, ensure_ascii=False)
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + f"…[已截断, 共{len(s)}字符]"


def _tool_input_str(input_: Any) -> str:
    """工具入参转字符串（供前端展示摘要）。"""
    if isinstance(input_, dict):
        try:
            return json.dumps(input_, ensure_ascii=False)
        except TypeError:
            return str(input_)
    return _truncate_text(input_, limit=200)


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _event_stream(req: ChatRequest) -> AsyncIterator[str]:
    """SSE 事件流: 逐 token 转发模型输出 + 工具调用事件。

    持久化模式(thread_id): config 带 thread_id, checkpointer 自动恢复/保存
    完整会话状态（消息、虚拟文件系统、中间步骤），前端无需回传 history。

    事件类型:
      token      - 模型文本增量
      tool_start - 工具开始 (id/name/input)
      tool_end   - 工具结束 (id/name/output)
      done/error - 结束或错误
    """
    try:
        input_state: dict[str, Any] = {"messages": [HumanMessage(content=req.message + _attach_desc(req))]}
        # LocalShellBackend: 记忆/技能文件直接走磁盘, 无需 files 注入
        agent = get_agent(req.model)  # 按请求模型取 agent（懒构建 + 缓存）

        if req.thread_id:
            # 服务端会话保存模式
            config = {"configurable": {"thread_id": req.thread_id}}
            event_iter = agent.astream_events(input_state, config=config, version="v2")
        else:
            # 兼容旧客户端: 前端回传 history 的无状态模式
            input_state["messages"] = _build_messages(req)
            event_iter = agent.astream_events(input_state, version="v2")

        async for event in event_iter:
            etype = event["event"]
            data = event.get("data", {})
            run_id = event.get("run_id", "")

            if etype == "on_tool_start":
                # 工具调用开始: 展示工具名与入参
                name = data.get("name") or data.get("input", {}).get("__name__", "tool")
                yield _sse({
                    "type": "tool_start",
                    "id": run_id,
                    "name": name,
                    "input": _tool_input_str(data.get("input")),
                })
            elif etype == "on_tool_end":
                # 工具调用结束: 展示输出（截断）; 真实工具名取自 ToolMessage.name
                out = data.get("output")
                real_name = getattr(out, "name", None) or data.get("name") or "tool"
                out_text = getattr(out, "content", out)
                yield _sse({
                    "type": "tool_end",
                    "id": run_id,
                    "name": real_name,
                    "output": _truncate_text(out_text),
                })
            elif etype == "on_chat_model_stream":
                text = _chunk_text(data.get("chunk"))
                if not text:
                    continue
                yield _sse({"type": "token", "content": text})
                await asyncio.sleep(0)  # 让出事件循环, 保证流式平滑
        yield _sse({"type": "done"})
    except Exception as exc:  # noqa: BLE001 - 流中断时通知前端
        yield _sse({"type": "error", "content": str(exc)})


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _parse_docx(path: str) -> str:
    """解析 .docx（zip 压缩的 XML）提取纯文本。"""
    import re
    import zipfile

    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        texts = []
        for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
            t = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para))
            texts.append(t)
        result = "\n".join(t for t in texts if t.strip())
        return result or "（未能从 docx 中提取到文本，可能是扫描件/图片型文档）"
    except Exception as exc:  # noqa: BLE001
        return f"（docx 解析失败: {exc}）"


def _parse_pdf(path: str) -> str:
    """解析 .pdf 提取纯文本。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        parts = []
        for page in reader.pages:
            try:
                t = page.extract_text()
                if t:
                    parts.append(t)
            except Exception:  # noqa: BLE001
                continue
        return "\n".join(parts) or "（未能从 PDF 中提取到文本，可能是扫描件/图片型 PDF）"
    except Exception as exc:  # noqa: BLE001
        return f"（PDF 解析失败: {exc}）"


def _parse_upload(path: str, name: str) -> str | None:
    """按扩展名解析上传文件为纯文本, 返回解析文本; 不支持的格式返回 None。"""
    ext = os.path.splitext(name)[1].lower()
    if ext in (".docx", ".docm"):
        return _parse_docx(path)
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext in (".txt", ".md", ".markdown", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".html", ".css", ".xml", ".yaml", ".yml", ".log", ".ini", ".conf", ".sh", ".sql", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".java"):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:  # noqa: BLE001
            return None
    return None  # 其他二进制格式不解析


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """附件上传: 保存到项目 uploads/ 目录, 返回虚拟路径供 Agent read_file。

    常见文档 (.docx/.pdf/文本类) 会额外解析为纯文本, 生成 .parsed.txt,
    返回 parsed_path 供 Agent 直接读取分析。
    """
    import uuid

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        return JSONResponse({"error": f"文件超过 {MAX_UPLOAD_SIZE // (1024 * 1024)}MB 限制"}, status_code=400)
    if not content:
        return JSONResponse({"error": "文件为空"}, status_code=400)

    name = os.path.basename(file.filename or "file").strip() or "file"
    safe = f"{uuid.uuid4().hex[:8]}_{name}"
    path = os.path.join(UPLOAD_DIR, safe)
    with open(path, "wb") as f:
        f.write(content)

    result: dict[str, Any] = {"name": name, "path": f"/uploads/{safe}", "size": len(content)}
    # 文档解析: 生成 .parsed.txt
    parsed = _parse_upload(path, name)
    if parsed is not None:
        parsed_name = safe + ".parsed.txt"
        with open(os.path.join(UPLOAD_DIR, parsed_name), "w", encoding="utf-8") as f:
            f.write(parsed)
        result["parsed_path"] = f"/uploads/{parsed_name}"
    return result


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Deep Agent Web Chat 服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
