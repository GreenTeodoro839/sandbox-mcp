# sandbox-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![image: GHCR](https://img.shields.io/badge/image-ghcr.io%2Fgreenteodoro839%2Fsandbox--mcp--base-2496ED?logo=docker&logoColor=white)](https://github.com/GreenTeodoro839/sandbox-mcp/pkgs/container/sandbox-mcp-base)
[![build image](https://github.com/GreenTeodoro839/sandbox-mcp/actions/workflows/build-image.yml/badge.svg)](https://github.com/GreenTeodoro839/sandbox-mcp/actions/workflows/build-image.yml)

给 AI 助手（如小米 Miclaw）用的**自托管 MCP 服务器**：在你自己的 Debian 机器上，用 Docker 容器作为沙箱，提供**命令执行、后台长任务、大文件传输、多沙箱自动管理**。

> 📱 **手机端搭档**：[sandbox-mcp-bridge](https://github.com/GreenTeodoro839/sandbox-mcp-bridge) —— 跑在手机上的极简 MCP 模块，给没有 `curl` 的手机补上"本机文件收发"能力，配合本服务的签名 URL 一起用。

> 状态：已在真实机器（Raspberry Pi / Debian + frp）上端到端跑通——握手、命令执行、后台任务、签名 URL 上传下载均验证过。你自己部署时仍可能要按 MCP SDK / docker-py 的版本细节小修小补。

## 它解决什么

手机上的 Miclaw 支持自定义 MCP 服务器（URL 型），但手机没有 Linux 环境执行命令。本服务把一台 Debian 机器变成沙箱后端，客户端通过一个 HTTPS 端点访问。

> 本服务只监听一个本地 HTTP 端口（默认 `127.0.0.1:8000`）。**怎么把它暴露成公网 HTTPS 端点由你决定**——nginx / Caddy 反向代理、Cloudflare Tunnel、frp、Tailscale 等任选其一，不在本项目范围内。

## 架构

```
小米手机 Miclaw
   │  MCP over HTTPS  →  https://你的域名/mcp   (Bearer token 鉴权)
   │  大文件 HTTP     →  https://你的域名/files/... (签名 URL，旁路)
   ▼  (反向代理 / 隧道 终止 TLS，单端口)
[Debian 主机]  sandbox-mcp (一个 ASGI 应用 / 一个端口)
   │  Docker SDK
   ▼
  容器=沙箱   smcp-projA   smcp-projB  ...
              每个挂载  DATA_DIR/<name>/workspace → /workspace
```

**关键设计**：大文件**不走** MCP 工具参数（会爆 AI 上下文），而是工具返回一个 HMAC 签名 URL，字节走普通 HTTP，直接读写沙箱挂载在宿主机上的 workspace 目录。

## MCP 工具

| 工具 | 作用 |
|---|---|
| `list_sandboxes` / `create_sandbox` / `destroy_sandbox` | 沙箱管理（exec 会按需自动建） |
| `exec(sandbox, command, timeout)` | 跑命令并等待结果（秒级用） |
| `run_background(sandbox, command)` → `job_id` | 启动长任务，立即返回 |
| `get_job(job_id)` / `stop_job(job_id)` | 查看进度日志 / 停止 |
| `list_files` / `read_text` / `write_text` | 列目录 / 读写**小**文本（脚本、配置） |
| `upload_url(sandbox, dest)` | 拿**上传**大文件的签名 URL（PUT） |
| `download_url(sandbox, src)` | 拿**下载**大文件的签名 URL（GET） |
| `fetch_url(sandbox, url, dest)` | 让沙箱自己 curl 一个网址进来（全速） |

典型流程（传多个 PDF 然后处理）：
1. AI 多次 `upload_url` 拿链接 → `curl -T a.pdf '<url>'` 把 PDF 推进沙箱
2. AI `write_text` 写处理脚本 → `run_background` 跑
3. `get_job` 轮询 → 完成后 `download_url` 给你结果链接

## 部署（在 Debian 主机上）

```bash
# 0. 装 Docker（若没有）
curl -fsSL https://get.docker.com | sh

# 1. 取代码到 /opt/sandbox-mcp
sudo mkdir -p /opt/sandbox-mcp && cd /opt/sandbox-mcp
# 把本仓库内容放进来（git clone / scp 均可）

# 2. 取沙箱基础镜像：已发布到 GHCR（amd64/arm64），无需自建。
#    .env 里 SMCP_BASE_IMAGE 默认就指向它，首次建沙箱时 Docker 会自动拉取。
#    （想自建就： docker build -t sandbox-mcp-base:latest images/ -f images/Dockerfile.base
#     并把 .env 的 SMCP_BASE_IMAGE 改回 sandbox-mcp-base:latest）
docker pull ghcr.io/greenteodoro839/sandbox-mcp-base:latest

# 3. Python 环境
python3 -m venv .venv
.venv/bin/pip install -e .

# 4. 配置
cp .env.example .env
# 编辑 .env：SMCP_TOKEN（openssl rand -hex 32）、SMCP_PUBLIC_BASE_URL
sudo mkdir -p /var/lib/sandbox-mcp

# 5. 起服务（systemd）
sudo cp deploy/sandbox-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sandbox-mcp
sudo systemctl status sandbox-mcp

# 自检
curl -s http://127.0.0.1:8000/healthz   # -> {"ok":true}
```

### 暴露成 HTTPS 端点

本服务只听一个本地端口，且 `/mcp` 和 `/files` 共用它，所以**对外只需暴露这一个端口**。用任意方式给它套上 TLS、转到 `SMCP_PORT` 即可，例如：

- **反向代理**（nginx / Caddy）：在 443 终止 TLS，`proxy_pass http://127.0.0.1:8000;`
- **隧道**（Cloudflare Tunnel / frp / Tailscale）：把公网域名指到本地 `8000`

无论哪种，把最终的公网地址填进 `SMCP_PUBLIC_BASE_URL`（用于生成文件签名 URL）。

> 注意：如果代理会改写 `Host` 头，本服务已默认关闭 MCP 传输层的 DNS-rebinding 保护以兼容，访问仍由 Bearer token 把关。

### 在 Miclaw 里添加（URL 型 MCP）

Miclaw 一次只能加一个 server。直接粘贴下面这条，把尖括号占位符换成你的实际值（具体字段名以 Miclaw 实际要求为准）：

```json
{
  "mcpServers": {
    "sandbox": {
      "url": "https://你的域名/mcp",
      "headers": {
        "Authorization": "Bearer <SMCP_TOKEN>"
      }
    }
  }
}
```

`<SMCP_TOKEN>` 用 `.env` 里的 `SMCP_TOKEN`，URL 换成你暴露出去的公网 HTTPS 地址。

> 如果还装了[手机端桥接器](https://github.com/GreenTeodoro839/sandbox-mcp-bridge)，再单独添加它那一条（配置模板见该仓库 README）。

## 配置（环境变量）

全部配置走环境变量，systemd 从 `.env` 加载（见 `.env.example`）。

**鉴权 / 地址**

| 变量 | 默认 | 作用 |
|---|---|---|
| `SMCP_TOKEN` | *(必填)* | 客户端访问 `/mcp` 必须带的 Bearer token；也是文件 URL 的默认签名密钥。用 `openssl rand -hex 32` 生成。 |
| `SMCP_SIGNING_KEY` | = `SMCP_TOKEN` | 单独的文件 URL HMAC 签名密钥；想和访问 token 分离时才设。 |
| `SMCP_PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 对外可见的地址，用来拼出文件上传/下载的签名 URL。**部署后必须改成你的公网 HTTPS 地址**，否则给出的链接客户端打不开。 |

**监听**

| 变量 | 默认 | 作用 |
|---|---|---|
| `SMCP_HOST` | `127.0.0.1` | 服务监听地址。建议保持 `127.0.0.1`，由反向代理/隧道对外。 |
| `SMCP_PORT` | `8000` | 服务监听端口（`/mcp` 与 `/files` 共用）。 |

**存储**

| 变量 | 默认 | 作用 |
|---|---|---|
| `SMCP_DATA_DIR` | `/var/lib/sandbox-mcp/data` | 每个沙箱的 `workspace` 目录所在；文件旁路直接读写这里。 |
| `SMCP_STATE_DB` | `/var/lib/sandbox-mcp/state.db` | SQLite，记录沙箱与后台任务的元数据（最后使用时间等）。 |

**沙箱容器**

| 变量 | 默认 | 作用 |
|---|---|---|
| `SMCP_BASE_IMAGE` | GHCR 已发布镜像 | 自动建沙箱用的镜像（`create_sandbox` 可单独指定别的）。`.env.example` 默认指向 `ghcr.io/greenteodoro839/sandbox-mcp-base:latest`；代码内置回退为本地 `sandbox-mcp-base:latest`。 |
| `SMCP_SANDBOX_NETWORK` | `bridge` | 沙箱网络模式；设 `none` 完全断网。 |
| `SMCP_MEM_LIMIT` | `2g` | 单沙箱内存上限。 |
| `SMCP_CPUS` | `2` | 单沙箱 CPU 核数上限。 |
| `SMCP_PIDS_LIMIT` | `512` | 单沙箱进程数上限（防 fork 炸弹）。 |
| `SMCP_MAX_SANDBOXES` | `20` | 同时存在的沙箱数上限，超出时新建会被拒。 |

**生命周期 / 超时（秒）**

| 变量 | 默认 | 作用 |
|---|---|---|
| `SMCP_IDLE_STOP_SECONDS` | `7200` (2h) | 沙箱空闲多久后**停止**容器（文件保留，下次用自动重启）。 |
| `SMCP_IDLE_REMOVE_SECONDS` | `604800` (7d) | 空闲多久后**删除**沙箱并清空其文件目录。 |
| `SMCP_GC_INTERVAL_SECONDS` | `300` | 后台 GC 巡检间隔。 |
| `SMCP_EXEC_TIMEOUT` | `60` | `exec` 的默认超时（调用时传 `timeout` 可覆盖）。 |
| `SMCP_JOB_TIMEOUT` | `3600` | `run_background` 任务的默认超时。 |
| `SMCP_JOB_LOG_RETENTION` | `86400` (1d) | 后台任务日志保留多久后由 GC 清理。 |
| `SMCP_URL_TTL` | `3600` | 文件上传/下载签名 URL 的有效期。 |
| `SMCP_READ_TEXT_MAX_BYTES` | `200000` | `read_text` 内联读取的大小上限，超过应改用 `download_url`。 |

## 安全须知

- `/mcp` = 远程任意命令执行入口，**必须**有 token + TLS（在你的反向代理 / 隧道上终止 TLS）。token 用足够长的随机串。
- 文件 URL 由 HMAC 签名 + 有效期（默认 1h）保护，不需要在浏览器里带 token。
- 沙箱容器：非特权、`no-new-privileges`、drop 部分 capability、限制内存/CPU/进程数。网络默认 bridge；想完全断网把 `SMCP_SANDBOX_NETWORK=none`。
- 服务以 root 运行：因为它要管 Docker（本就等价 root）并读取容器以 root 身份产生的文件，不增加额外攻击面。机器请专用于此用途。

## 待办 / 部署时要验证

- MCP SDK 的 `FastMCP.streamable_http_app()` 与默认 `/mcp` 路径在你装的版本上一致（不一致就调 `build_app()` 里的前缀）。
- docker-py 与 Docker daemon 连通（`docker.from_env()`）。
- `run_background` 的 `setsid` 进程组停止逻辑在你的基础镜像里按预期工作。
- 大文件上传/下载经过你的反向代理 / 隧道时的实际吞吐。
