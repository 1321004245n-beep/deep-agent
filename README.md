# Deep Agent Web Chat

基于 **deepagents 0.7.4 + DeepSeek** 的智能体对话系统。Web 界面聊天，Agent 具备完整的工具链能力。

## ✨ 能力

- 💬 流式对话（SSE）+ Markdown 渲染 + 代码高亮
- 🔧 **文件系统**（读写改删/搜索）+ **Shell 执行**（execute）
- 🔍 **联网搜索**（web_search）+ **网页抓取**（fetch_webpage）
- 🧠 **长期记忆**自动沉淀（store + save_memory/list_memories）
- 📁 **附件上传**（Word/PDF 自动解析为纯文本）
- 🛠 **技能系统**（Skills：code-review / debugging / git-workflow）
- 🕐 日期时间感知（自动注入）
- 🔄 模型切换（deepseek-v4-flash / deepseek-chat / deepseek-reasoner）
- 💾 会话持久化（SQLite，重启不丢）

## 🚀 本地运行

```bash
# 1. 环境
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 2. 配置（填 DeepSeek API Key）
cp .env.example .env

# 3. 启动
.venv/bin/python web_server.py --port 8000
# 打开 http://127.0.0.1:8000
```

## 🌐 服务器部署

详见 [`deploy/README.md`](deploy/README.md)：systemd 守护 + nginx 反代（SSE）完整步骤。

## 📁 结构

```
web_server.py      服务入口（FastAPI + SSE）
agent_service.py   Agent 构建（deepagents + DeepSeek）
web/               前端（原生 HTML/CSS/JS）
memory/            长期记忆（AGENTS.md）
skills/            技能库
deploy/            部署辅助（systemd / nginx / 说明）
```

## ⚠️ 安全注意

- `.env` 中的 **DeepSeek API Key 严禁提交**（已 gitignore）
- Agent 具备本机 Shell 执行能力（execute 无沙箱）——仅个人/内网使用；公网部署务必加鉴权
