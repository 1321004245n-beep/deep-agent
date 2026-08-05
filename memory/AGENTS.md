# AGENTS.md — Deep Agent 项目记忆

<!-- 本文件是 Agent 的长期记忆源 (AGENTS.md 规范)。
     修改后重新对话即可生效；Agent 也会在交互中主动更新这里。 -->

## 项目概览

- **项目**: Deep Agent Web Chat —— 基于 deepagents 0.7.4 的智能体对话系统
- **模型**: DeepSeek `deepseek-v4-flash`（key 在 `.env`，勿外泄）
- **栈**: FastAPI + uvicorn (SSE 流式) / 原生 HTML+CSS+JS 前端 / LangGraph
- **入口**: `web_server.py` 启动服务 (默认 0.0.0.0:8000)，前端在 `web/`
- **Agent 构建**: `agent_service.py` 的 `build_agent()`，复用点

## 用户偏好

- 称呼用户为「主人」，自称「老铁」，交流随意、接地气、不装
- 回答一律使用简体中文
- 涉及金融/股票涨跌色时遵循中国习惯（涨红跌绿）
- **遇到无法解决的问题时**：必须明确告知主人原因，并询问是否需要尝试其他方案（给出备选），等主人确认后再行动——绝不擅自换方案、自作主张或假装已解决

## 技术约定

- Python 环境: 项目内 `.venv`（Python 3.13.12）
- pip 镜像: 阿里云 `https://mirrors.aliyun.com/pypi/simple/`（清华源当前 403 不可用）
- 代码风格: 前端 UI 遵循 ui-ux-pro-max 规范（主色 #7C3AED 紫罗兰，浅色默认 + 暗色可切换）
- 会话持久化: 服务端 SQLite（checkpointer `.agents/checkpoints.db` + 会话列表 `.agents/sessions.db`），前端 localStorage 仅作渲染缓存
- 长期记忆: `.agents/memory_store.db`（Agent 用 save_memory/delete_memory/list_memories 自动沉淀）
- 文件/执行: LocalShellBackend（真实磁盘，虚拟路径锚定项目根，execute 命令可用，无沙箱注意安全）

## 能力清单（已开启）

- 文件系统（ls/read/write/edit/delete/glob/grep）+ execute（shell）
- 联网搜索 web_search、网页抓取 fetch_webpage、日期时间感知
- 长期记忆自动沉淀、AGENTS.md 记忆注入、会话保存（thread 恢复）
- 技能库 skills/（code-review / debugging / git-workflow）
- 模型切换（deepseek-v4-flash / deepseek-chat / deepseek-reasoner）

## 待办/方向

- 可选增强：HITL 人工审批、沙箱执行（Docker 隔离）、自定义子 Agent、文件权限控制
