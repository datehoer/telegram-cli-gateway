# 三 Bot 隔离实验环境

该 Compose 栈为 Codex、Claude Code 和 Grok 各运行一个独立的 gateway。每个服务只有自己的 CLI、网络、HOME、工作区、runtime、认证和 Telegram Bot。

## Bot 映射

- `codex`：`@OpenClaw30w2w_bot`
- `claude`：`@jr_claude_code_bot`
- `grok`：`@myopenclaw_news_report_bot`

Bot Token 保存在被 Git 忽略的 `secrets/*.bot-token` 文件中。Compose 从项目根目录 `.env` 读取相同的 `TELEGRAM_ALLOWED_USERS`，不会把原 Bot Token传入实验容器。

CLI 认证从宿主机的最小认证文件或本目录的私密配置复制到各自 HOME；不会复制历史、缓存、记忆或旧会话。Token 不会写入镜像层或容器环境变量。

## 使用

从项目根目录运行：

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml build
docker compose --env-file .env -f docker/experiments/compose.yaml up -d
docker compose --env-file .env -f docker/experiments/compose.yaml ps
```

首次给三个 Bot 分别发送：

```text
/new codex
/new claude
/new grok
```

每个工作区首次启动时从同一个镜像快照复制到 `/workspace/project`，并创建只有一个基线提交的新 Git 仓库。候选代码和 CLI 会话保存在各自命名卷中，容器重启不会丢失。

查看日志：

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml logs -f codex
```

导出候选补丁：

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml exec codex \
  git -C /workspace/project diff --binary HEAD
```

停止容器不会删除结果：

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml stop
```

不要使用 `down -v`，除非明确准备删除三个实验的工作区、会话和 runtime。

本机旧的 `telegram-cli-gateway.service` 使用 Codex 实验 Bot 的同一个 Token，因此三容器栈运行期间必须保持禁用。回退到原服务时先停止容器，再恢复服务：

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml stop
systemctl --user enable --now telegram-cli-gateway.service
```

轮换任一 Bot 或模型 API Token 后，更新对应的 `docker/experiments/secrets/` 文件并重建容器配置：

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml up -d --force-recreate
```
