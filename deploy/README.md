# Deep Agent Web Chat — 部署说明

## 一、文件清单

```
web_server.py      # 服务入口 (FastAPI + SSE)
agent_service.py   # Agent 构建 (deepagents + DeepSeek)
requirements.txt   # Python 依赖 (95 个, 已锁定)
.env.example       # 环境变量模板 (复制为 .env 填写)
web/               # 前端 (原生 HTML/CSS/JS, 无需构建)
memory/            # 长期记忆 (AGENTS.md)
skills/            # 技能库 (code-review / debugging / git-workflow)
deploy/            # 部署辅助 (systemd / nginx / 本说明)
```

## 二、快速部署 (Ubuntu/Debian 示例)

```bash
# 1) 环境
sudo apt update && sudo apt install -y python3.12 python3.12-venv nginx

# 2) 代码
sudo mkdir -p /opt/deep-agent
sudo cp -r . /opt/deep-agent/          # 本目录全部内容
sudo chown -R $USER:$USER /opt/deep-agent

# 3) 依赖
cd /opt/deep-agent
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 4) 配置
cp .env.example .env
vim .env                               # 填入 DEEPSEEK_API_KEY (必填)

# 5) systemd 守护 (崩溃自启 + 开机自启)
sudo cp deploy/deep-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deep-agent

# 6) 验证
curl -s http://127.0.0.1:8000/ | head          # 本机 OK
journalctl -u deep-agent -f                    # 看日志

# 7) (可选) nginx 反代 + HTTPS
sudo cp deploy/nginx.conf /etc/nginx/conf.d/deep-agent.conf
# 修改 server_name / 证书路径后:
sudo nginx -t && sudo systemctl reload nginx
```

## 三、防火墙

- 直连: 放行 `8000`
- nginx: 仅放行 `80/443`（推荐）

## 四、⚠️ 安全须知

1. **execute 无沙箱**：Agent 可执行任意 shell 命令。仅建议**个人/内网**使用；
   公网部署前务必增加人工审批（HITL）或改 Docker 沙箱后端。
2. **无鉴权**：页面无登录。公网必须加访问密码或 IP 白名单。
3. **DeepSeek Key**：放服务器 `.env`，不要提交到任何版本库；定期轮换。
4. **数据**：`.agents/`（会话/记忆/检查点）与 `uploads/`（附件）自动创建，
   建议定期备份 `.agents/`。

## 五、常用运维

```bash
sudo systemctl status deep-agent      # 状态
sudo systemctl restart deep-agent     # 重启
journalctl -u deep-agent -n 100       # 最近日志
```
