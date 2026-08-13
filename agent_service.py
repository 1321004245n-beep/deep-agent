#!/usr/bin/env python3
"""基础 Agent 服务 —— 基于 deepagents + DeepSeek 模型

功能:
  - 构建一个开箱即用的 Deep Agent（内置文件系统、Shell、任务规划等工具）
  - 模型使用 DeepSeek（langchain-deepseek / ChatDeepSeek）
  - API Key 预留: 从环境变量 DEEPSEEK_API_KEY 读取, 也可通过 --api-key 传入

用法:
  1) 配置 Key（三选一）:
     cp .env.example .env 并填入 DEEPSEEK_API_KEY   # 推荐, 自动加载
     export DEEPSEEK_API_KEY=sk-xxxxxxxx            # 环境变量
     python agent_service.py --api-key sk-xxx       # 临时传入

  2) 启动:
     python agent_service.py                      # 进入交互式对话
     python agent_service.py "帮我写一个快速排序"   # 单次问答

  3) 在代码中复用:
     from agent_service import build_agent
     agent = build_agent()
     result = agent.invoke({"messages": "你好"})
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Annotated, Any, NotRequired, Optional, TypedDict

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    ResponseT,
)
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI  # 智谱 GLM (OpenAI 兼容接口)
from deepagents import create_deep_agent

# ---------------------------------------------------------------------------
# 配置: 模型与 Key 通过 .env 文件或环境变量注入
#   .env 示例见 .env.example; 实际 .env 已被 .gitignore 忽略, 不会泄露
# ---------------------------------------------------------------------------
DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")  # 当前: deepseek-v4-flash
DEFAULT_TEMPERATURE = 0.7
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")


def load_env_file(env_file: str = ENV_FILE) -> None:
    """轻量加载 .env 文件到进程环境变量（不覆盖已存在的变量）。"""
    if not os.path.exists(env_file):
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


@tool
def get_current_time() -> str:
    """获取当前日期和时间。当用户询问时间或日期时使用。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@tool
def fetch_webpage(url: str, max_chars: int = 8000) -> str:
    """抓取指定网页内容并转换为纯文本。

    当用户要求获取网页信息、查资料、看文章内容时使用。会自动处理编码
    （中文页面正常）并剥离 HTML 标签/脚本/样式。

    Args:
        url: 完整网址（必须以 http:// 或 https:// 开头）。
        max_chars: 返回内容的最大字符数, 默认 8000, 最大 30000。
    """
    import re

    import requests

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return "错误: URL 必须以 http:// 或 https:// 开头。"
    max_chars = max(500, min(int(max_chars or 8000), 30000))
    try:
        resp = requests.get(
            url,
            timeout=20,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                )
            },
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return f"错误: 请求超时 (20s) - {url}"
    except requests.exceptions.RequestException as exc:
        return f"错误: 请求失败 - {exc}"

    # 编码: 优先服务器声明, 兜底自动检测(中文页面)
    try:
        resp.encoding = resp.apparent_encoding or resp.encoding
    except Exception:  # noqa: BLE001
        pass
    html = resp.text

    # HTML → 纯文本
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except Exception:  # noqa: BLE001 - bs4 不可用时退化为正则清洗
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html)
        text = re.sub(r"<[^>]+>", " ", text)

    # 压缩空白
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if not text:
        return f"提示: 页面 {url} 无可见文本内容（可能是 JS 渲染页面）。"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n…[内容过长已截断, 全文 {len(text)} 字符]"
    return text


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索关键词，返回相关网页的结果列表（标题 / 链接 / 摘要）。

    当用户需要查找最新信息、搜索资料、了解某个话题时使用。搜索结果给出
    链接与摘要，需要深入了解某条结果时，再用 fetch_webpage 抓取正文。

    Args:
        query: 搜索关键词（中文/英文均可）。
        max_results: 返回结果条数, 默认 5, 最大 10。
    """
    import base64
    import urllib.parse

    import requests
    from bs4 import BeautifulSoup

    query = query.strip()
    if not query:
        return "错误: 搜索关键词不能为空。"
    max_results = max(1, min(int(max_results or 5), 10))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(
            "https://cn.bing.com/search",
            params={"q": query, "setlang": "zh-hans", "mkt": "zh-CN", "count": max_results},
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        return f"错误: 搜索请求失败 - {exc}"

    def _real_url(href: str) -> str:
        # Bing 部分结果是跳转链接(/ck/a?...&u=base64), 解码还原真实 URL
        if "bing.com/ck/a" in href:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            u = qs.get("u", [""])[0]
            if u:
                try:
                    return base64.urlsafe_b64decode(u + "==").decode()
                except Exception:  # noqa: BLE001
                    return href
        return href

    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[tuple[str, str, str]] = []
    for algo in soup.select("li.b_algo"):
        h = algo.select_one("h2 a") or algo.select_one("a")
        if not h:
            continue
        title = h.get_text(strip=True)
        url = _real_url(h.get("href") or "")
        snip_el = algo.select_one(".b_caption p") or algo.select_one("p")
        snippet = snip_el.get_text(strip=True) if snip_el else ""
        if title and url:
            results.append((title, url, snippet))
        if len(results) >= max_results:
            break

    if not results:
        return f"提示: 未搜索到「{query}」相关结果，可尝试更换关键词。"

    lines = [f"「{query}」搜索结果（共 {len(results)} 条）:"]
    for i, (title, url, snippet) in enumerate(results, 1):
        lines.append(f"{i}. {title}\n   链接: {url}\n   摘要: {snippet}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 记忆 (Memory) 配置: AGENTS.md 作为长期记忆源, 注入系统提示词
#   - 虚拟路径是 StateBackend 视角的路径
#   - 本地文件路径是项目内真实文件
# ---------------------------------------------------------------------------
MEMORY_VIRTUAL_PATH = "/memory/AGENTS.md"
MEMORY_LOCAL_PATH = os.path.join(BASE_DIR, "memory", "AGENTS.md")


# ---------------------------------------------------------------------------
# 长期记忆 (Long-Term Memory) 自动沉淀
#   - store: SQLite 持久化键值库 (.agents/memory_store.db), 跨会话共享
#   - 工具: save_memory / delete_memory / list_memories, Agent 自行读写
#   - 中间件: LongTermMemoryMiddleware 每次对话把记忆注入系统提示词
# ---------------------------------------------------------------------------
LTM_STORE_PATH = os.path.join(BASE_DIR, ".agents", "memory_store.db")
LTM_NAMESPACE = ("agent", "memories")

# ---------------------------------------------------------------------------
# 技能 (Skills) 配置: 渐进式披露的技能库
#   - 本地 skills/ 目录, 每个技能 = 子目录 + SKILL.md (YAML frontmatter)
#   - StateBackend 下通过 invoke(files=...) 注入, SkillsMiddleware 扫描并
#     把技能清单注入系统提示, Agent 需要时用 read_file 读 SKILL.md 全文
# ---------------------------------------------------------------------------
SKILLS_LOCAL_DIR = os.path.join(BASE_DIR, "skills")
SKILLS_VIRTUAL_ROOT = "/skills"


def load_skill_files() -> dict[str, dict[str, str]]:
    """读取本地技能目录全部文件, 映射为虚拟路径注入字典（供 StateBackend）。"""
    if not os.path.isdir(SKILLS_LOCAL_DIR):
        return {}
    result: dict[str, dict[str, str]] = {}
    for root, _, files in os.walk(SKILLS_LOCAL_DIR):
        for fn in files:
            local = os.path.join(root, fn)
            rel = os.path.relpath(local, SKILLS_LOCAL_DIR).replace(os.sep, "/")
            vpath = f"{SKILLS_VIRTUAL_ROOT}/{rel}"
            with open(local, encoding="utf-8") as f:
                result[vpath] = {"content": f.read(), "encoding": "utf-8"}
    return result

LTM_SYSTEM_PROMPT = """<long_term_memory>
{long_term_memory}

</long_term_memory>

<memory_guidelines>
    The above <long_term_memory> was accumulated across all your conversations. It contains
    durable user preferences, corrections, and facts worth remembering beyond the current thread.

    - Trust but verify: memory may be outdated or context-dependent. Prefer the user's explicit
      request and verified evidence when they conflict.
    - When the user states a preference, corrects you, or shares a durable fact, call
      `save_memory(topic, content)` to persist it — usually in the same turn.
    - Use `delete_memory(topic)` to remove stale or wrong entries; use `list_memories()` to
      review what you know before answering when relevant.
</memory_guidelines>"""


def create_memory_store():
    """创建长期记忆 SQLite store（同步版, 连接生命周期随进程）。"""
    import sqlite3

    from langgraph.store.sqlite import SqliteStore

    os.makedirs(os.path.dirname(LTM_STORE_PATH), exist_ok=True)
    # isolation_level=None 让 store 内部自行管理事务 (BEGIN/COMMIT)
    conn = sqlite3.connect(LTM_STORE_PATH, check_same_thread=False, isolation_level=None)
    store = SqliteStore(conn)
    # 首次使用自动建表
    if hasattr(store, "setup"):
        store.setup()
    return store


def _make_memory_tools(store):
    """构建长期记忆工具（闭包持有 store 单例）。"""

    @tool
    def save_memory(topic: str, content: str) -> str:
        """保存一条跨会话长期记忆。当用户表达持久偏好、纠正你的行为、或分享值得长期记住的事实/约定时调用。
        topic: 简短主题词（如 "coding_style"、"user_preference"）; content: 具体记忆内容。"""
        store.put(LTM_NAMESPACE, topic, {"content": content})
        return f"已保存长期记忆: {topic}"

    @tool
    def delete_memory(topic: str) -> str:
        """删除一条长期记忆。当记忆过时、错误或用户明确要求遗忘时调用。topic: 记忆的主题词。"""
        store.delete(LTM_NAMESPACE, topic)
        return f"已删除长期记忆: {topic}"

    @tool
    def list_memories() -> str:
        """列出当前全部长期记忆。回答与用户历史偏好/约定相关的问题前可先查看。"""
        items = store.search(LTM_NAMESPACE, limit=100)
        if not items:
            return "（暂无长期记忆）"
        return "\n".join(f"- {it.key}: {it.value.get('content', '')}" for it in items)

    return [save_memory, delete_memory, list_memories]


class LTMState(AgentState):
    """长期记忆中间件的 state 扩展（私有字段, 不进入最终输出）。"""

    ltm_memories: NotRequired[Annotated[dict[str, str], PrivateStateAttr]]


_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


class TimeAwarenessMiddleware(AgentMiddleware):
    """日期时间感知: 每次模型调用自动把当前时间注入系统提示词。

    Agent 无需调用工具即可感知"今天是几号、星期几、几点"，避免
    忘记查时间或依赖工具调用。精确到秒的时间换算仍可用 get_current_time 工具。
    """

    def _inject(self, request: ModelRequest) -> ModelRequest:
        now = datetime.now()
        injected = (
            f"<current_time>{now.strftime('%Y-%m-%d')} 星期{_WEEKDAYS[now.weekday()]} "
            f"{now.strftime('%H:%M:%S')} (GMT+8)</current_time>"
        )
        base = request.system_message.content if request.system_message else ""
        return request.override(
            system_message=SystemMessage(
                content=f"{base}\n\n{injected}" if base else injected
            )
        )

    def wrap_model_call(self, request, handler):  # noqa: ANN001
        return handler(self._inject(request))

    async def awrap_model_call(self, request, handler):  # noqa: ANN001
        return await handler(self._inject(request))


class LongTermMemoryMiddleware(AgentMiddleware):
    """把长期记忆注入每次模型调用的系统提示词。

    机制（仿 MemoryMiddleware）:
      - before_agent: 从 store 读全部记忆, 存入 state 私有字段（仅一次）
      - wrap_model_call: 调用 _inject 把记忆拼入 system message
    """

    state_schema = LTMState

    def __init__(self, store):
        self._store = store

    def _load(self) -> dict[str, str]:
        try:
            items = self._store.search(LTM_NAMESPACE, limit=100)
        except Exception:  # noqa: BLE001 - store 不可用时退化为无记忆
            return {}
        return {it.key: it.value.get("content", "") for it in items}

    def before_agent(self, state: LTMState, runtime, config):  # noqa: ANN001
        if state.get("ltm_memories") is not None:
            return None
        return {"ltm_memories": self._load()}

    async def abefore_agent(self, state: LTMState, runtime, config):  # noqa: ANN001
        if state.get("ltm_memories") is not None:
            return None
        return {"ltm_memories": self._load()}

    def _inject(self, request: ModelRequest) -> ModelRequest:
        memories = request.state.get("ltm_memories") or {}
        if not memories:
            return request
        body = "\n".join(f"- {topic}: {content}" for topic, content in memories.items())
        injected = LTM_SYSTEM_PROMPT.format(long_term_memory=body)
        base = request.system_message.content if request.system_message else ""
        return request.override(
            system_message=SystemMessage(
                content=f"{base}\n\n{injected}" if base else injected
            )
        )

    def wrap_model_call(self, request, handler):  # noqa: ANN001
        return handler(self._inject(request))

    async def awrap_model_call(self, request, handler):  # noqa: ANN001
        return await handler(self._inject(request))


# ---------------------------------------------------------------------------
# 执行后端 (execute 能力): LocalShellBackend
#   - 真实磁盘文件系统, 虚拟路径锚定项目根 (virtual_mode=True, 阻止 ../ 穿越)
#   - execute 工具可在项目根运行 shell 命令（无沙箱, 见官方安全警告）
# ---------------------------------------------------------------------------
def create_local_backend():
    """创建本地执行后端（真实磁盘 + shell 执行）。

    文件虚拟路径锚定项目根: /memory/AGENTS.md -> <项目根>/memory/AGENTS.md
    注意: execute 命令无沙箱隔离, 有权限即可任意执行 —— 生产环境勿用。
    """
    from deepagents.backends import LocalShellBackend

    return LocalShellBackend(root_dir=BASE_DIR, virtual_mode=True)


def load_memory_files() -> dict[str, dict[str, str]]:
    """读取本地记忆文件, 映射为 StateBackend 注入字典。

    StateBackend 是内存后端, 记忆文件需在每次 invoke 时通过
    `files={"虚拟路径": FileData}` 注入（FileData 含 content/encoding）。
    返回空 dict 表示未启用记忆。
    """
    if not os.path.exists(MEMORY_LOCAL_PATH):
        return {}
    with open(MEMORY_LOCAL_PATH, encoding="utf-8") as f:
        content = f.read()
    return {MEMORY_VIRTUAL_PATH: {"content": content, "encoding": "utf-8"}}


# ---------------------------------------------------------------------------
# 会话保存 (Checkpointer) 配置: SQLite 持久化, 服务端保存每个会话的完整状态
#   - thread_id 维度: 同一会话 id 可跨请求/跨重启恢复上下文与文件系统
#   - CLI 用同步 SqliteSaver, Web(FastAPI 异步) 用 AsyncSqliteSaver
# ---------------------------------------------------------------------------
CHECKPOINT_DB_PATH = os.path.join(BASE_DIR, ".agents", "checkpoints.db")


def create_sync_checkpointer():
    """创建同步 SQLite checkpointer（CLI/同步环境）。

    连接生命周期随进程（进程退出时自动关闭）。
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    os.makedirs(os.path.dirname(CHECKPOINT_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
    return SqliteSaver(conn)


async def create_async_checkpointer():
    """创建异步 SQLite checkpointer（FastAPI 环境, 必须在事件循环内调用）。

    返回 AsyncSqliteSaver, 连接由调用方管理（应用关闭时 await conn.close()）。
    """
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    os.makedirs(os.path.dirname(CHECKPOINT_DB_PATH), exist_ok=True)
    conn = await aiosqlite.connect(CHECKPOINT_DB_PATH)
    return AsyncSqliteSaver(conn)


def get_checkpointer(async_: bool = True):
    """兼容入口：同步环境直接返回; 异步环境请改用 create_async_checkpointer。"""
    if async_:
        msg = "异步 checkpointer 请在 async 上下文中使用 await create_async_checkpointer()"
        raise RuntimeError(msg)
    return create_sync_checkpointer()


def build_agent(
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = DEFAULT_TEMPERATURE,
    system_prompt: Optional[str] = None,
    memory: Optional[list[str]] = None,
    skills: Optional[list[str]] = None,
    checkpointer=None,
    store=None,
    backend=None,
    extra_middleware: Optional[list[Any]] = None,
):
    """构建 Deep Agent（DeepSeek 模型）。

    Args:
        api_key: DeepSeek API Key。None 时依次从 .env 文件 / 环境变量 DEEPSEEK_API_KEY 读取。
        model: 模型名, 默认从 .env 的 DEEPSEEK_MODEL 读取, 兜底 deepseek-chat。
        temperature: 采样温度。
        system_prompt: 自定义系统提示词。
        memory: 记忆源虚拟路径列表（AGENTS.md）。None 时默认加载 MEMORY_VIRTUAL_PATH。
        skills: 技能源虚拟路径列表。None 时默认加载本地 skills/ 目录（/skills/）。
        checkpointer: LangGraph Checkpointer（会话持久化）。
            None 时默认用同步 SqliteSaver（CLI 环境）；Web 异步环境请显式传
            create_async_checkpointer() 的结果。
        store: 长期记忆 store（BaseStore）。None 时默认创建同步 SQLite store，
            并自动挂载记忆工具 + LongTermMemoryMiddleware（自动沉淀）。
        backend: 文件/执行后端。None 时默认 LocalShellBackend(root_dir=项目根)，
            启用 execute（shell 命令）能力。

    Returns:
        可直接 .invoke() / .stream() 调用的 CompiledStateGraph agent。
    """
    load_env_file()
    # 模型路由: glm-* → 智谱 (ChatOpenAI 兼容), 其余 → DeepSeek
    model_name = model or os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL
    if model_name.startswith("glm-"):
        glm_key = os.getenv("GLM_API_KEY")
        if not glm_key:
            print(
                "\n⚠️  未检测到智谱 GLM API Key!\n"
                "    请设置环境变量 GLM_API_KEY, 例如:\n"
                "    export GLM_API_KEY=xxxxxxxx.xxxxxxxx\n"
                "    服务仍可构建, 但调用模型时会报错。\n"
            )
            glm_key = "placeholder-glm-api-key"
        model_instance = ChatOpenAI(
            model=model_name,
            temperature=temperature,
            api_key=glm_key,
            base_url="https://open.bigmodel.cn/api/paas/v4",
        )
    else:
        key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            print(
                "\n⚠️  未检测到 DeepSeek API Key!\n"
                "    请设置环境变量 DEEPSEEK_API_KEY（或 --api-key 传入），例如:\n"
                "    export DEEPSEEK_API_KEY=sk-xxxxxxxx\n"
                "    服务仍可构建, 但调用模型时会报错。\n"
            )
            # 占位 Key: 仅保证 agent 可构建, 实际调用会因认证失败给出明确报错
            key = "sk-placeholder-please-set-DEEPSEEK_API_KEY"
        model_instance = ChatDeepSeek(
            model=model_name,
            temperature=temperature,
            api_key=key,
        )

    # 会话持久化: 默认 SQLite checkpointer（同步版, 兼容 CLI）
    if checkpointer is None:
        checkpointer = create_sync_checkpointer()

    # 长期记忆: 默认启用（store + 记忆工具 + 注入中间件）
    tools_list: list[Any] = [get_current_time, fetch_webpage, web_search]
    middleware_list: list[Any] = []
    if store is None:
        store = create_memory_store()
    tools_list += _make_memory_tools(store)
    middleware_list.append(LongTermMemoryMiddleware(store))
    middleware_list.append(TimeAwarenessMiddleware())  # 日期时间感知
    if extra_middleware:
        middleware_list.extend(extra_middleware)

    # 执行后端: 默认 LocalShellBackend（真实磁盘 + execute shell 能力）
    if backend is None:
        backend = create_local_backend()

    # 当前模型身份注入: 让 Agent 能如实回答"你是什么模型"
    provider = "智谱 GLM" if model_name.startswith("glm-") else "DeepSeek"
    model_identity = (
        f"【当前模型】你本次对话调用的底层模型是 `{model_name}`（{provider}）。"
        "当用户询问你是什么模型、由哪个公司开发时, 请如实告知该模型名, "
        "不要擅自断言其他模型或根据记忆/代码猜测。\n\n"
    )
    user_prompt = system_prompt or (
        "你是一个能力全面的智能助手。你可以规划任务、读写文件、执行命令, "
        "并把复杂任务拆解成子任务完成。回答使用简体中文。\n\n"
        "你拥有跨会话的长期记忆能力: 当主人表达了持久的偏好、纠正了你的行为、"
        "或分享了值得长期记住的事实/约定时, 请用 save_memory 工具保存; "
        "记忆过时用 delete_memory 删除; 需要回顾时用 list_memories。\n\n"
        "你还拥有技能库（skills）: 技能清单已注入你的上下文, 当任务匹配某个技能时, "
        "先用 read_file 读取对应 SKILL.md 的完整步骤, 再按技能执行。\n\n"
        "你可以在本地执行 shell 命令（execute 工具）: 使用前先想清楚命令, "
        "避免破坏性操作; 需要安装依赖时优先用项目 .venv 的 python/pip。\n\n"
        "你还可以抓取网页（fetch_webpage 工具）: 当用户想了解某个网页的内容、"
        "查资料或看文章时, 用 fetch_webpage 抓取并总结。\n\n"
        "你可以联网搜索（web_search 工具）: 当用户询问最新信息、需要搜索资料、"
        "或话题需要外部信息时, 先用 web_search 搜索, 需要详情再用 fetch_webpage 抓正文。\n\n"
        "【重要行为准则】当遇到无法解决的问题、任务无法完成、或现有能力不足时: "
        "必须明确告知主人当前的情况和原因, 并询问是否需要尝试其他方案（给出可行的备选）, "
        "等待主人确认后再行动。绝不擅自更换方案、自作主张做主人没要求的事, "
        "也绝不假装问题已解决。"
    )

    agent = create_deep_agent(
        model=model_instance,
        tools=tools_list,  # 内置文件系统/Shell 等工具已默认启用
        middleware=middleware_list,
        backend=backend,                      # 本地执行后端 (execute 可用)
        skills=skills or [SKILLS_VIRTUAL_ROOT + "/"],  # 技能: 渐进式披露
        system_prompt=model_identity + user_prompt,
        memory=memory or [MEMORY_VIRTUAL_PATH],  # 启用记忆: 加载 AGENTS.md
        checkpointer=checkpointer,               # 会话保存: SQLite 持久化
        store=store,                             # 长期记忆: store 读写
        name="deepseek-agent",
    )
    return agent


def run_once(agent, question: str) -> str:
    """单次问答, 返回最终回答文本。"""
    # LocalShellBackend 下记忆/技能文件直接走磁盘, 无需 files 注入
    result = agent.invoke({"messages": question})
    return result["messages"][-1].content


def run_cli(agent) -> None:
    """交互式对话入口。"""
    print("=" * 60)
    print("  老铁 Agent 已就绪 🔧  模型: DeepSeek")
    print("  输入你的问题; 输入 /quit 或 Ctrl+C 退出")
    print("=" * 60)
    while True:
        try:
            question = input("\n🧑 主人: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见主人 👋")
            break
        if not question:
            continue
        if question.lower() in ("/quit", "/exit", "退出"):
            print("再见主人 👋")
            break
        print("\n🤖 老铁: ", end="", flush=True)
        try:
            answer = run_once(agent, question)
            print(answer)
        except Exception as exc:  # noqa: BLE001 - CLI 层兜底
            print(f"(调用失败: {exc})")


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    api_key = None
    questions: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--api-key" and i + 1 < len(argv):
            api_key = argv[i + 1]
            i += 2
        else:
            questions.append(arg)
            i += 1

    agent = build_agent(api_key=api_key)

    if questions:
        print(run_once(agent, " ".join(questions)))
        return 0
    run_cli(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
