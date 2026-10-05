# 本地 Bot API：1 GiB 文件发送

官方云端 Bot API 的文件上传限制为 50 MB。自建服务器必须以 `--local` 启动，
才能使用最大 2000 MB 上传和本机路径发送。网关的本地模式默认限制为 1 GiB，
照片仍按 10 MB 的照片发送规则处理。

官方说明：
- https://core.telegram.org/bots/api#using-a-local-bot-api-server
- https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server
- https://core.telegram.org/api/obtaining_api_id

## 凭据与服务准备

从 `https://my.telegram.org/apps` 获取自己的 Telegram `api_id` 和 `api_hash`。
这两个应用凭据与 BotFather 发放的 Bot Token 不同。将它们写入仓库根目录的
`.telegram-bot-api.env`，权限设为 `0600`；该文件被 Git 忽略，不要发送到聊天里。

```dotenv
TELEGRAM_API_ID=your_numeric_api_id
TELEGRAM_API_HASH=your_api_hash
```

部署文件适用于本工作区：`/srv/projects/telegram-cli-gateway`，用户 UID/GID 1000。
镜像从固定的官方源码版本构建，端口只绑定 `127.0.0.1:8081`。容器和网关使用相同的
绝对文件路径：项目文件只读，本地 Bot API 自己的状态目录可写。状态目录内也含私有数据。

```bash
chmod 600 .telegram-bot-api.env
mkdir -p .runtime/local-bot-api/tmp
chmod 700 .runtime/local-bot-api .runtime/local-bot-api/tmp
docker compose -f docker/local-bot-api/compose.yaml build
docker compose -f docker/local-bot-api/compose.yaml up -d
```

启动容器不会自动修改网关配置，也不会注销云端机器人。

## 切换与验证

先运行完整测试，并在所有原生 CLI 回合完成后切换。空闲检查
必须能读取 `.runtime/state.json`，且 `in_flight` 连续多次为空；超时或读失败则中止。

1. 停止 `telegram-cli-gateway.service` 的轮询，保留原配置备份。
2. 使用私有配置加载器读取所有已配置 Bot Token；对每个云端机器人调用 `logOut`。
   不把 Token 放入命令行、不输出请求 URL、不清空更新队列。未成功注销的机器人应停止切换。
3. 在 `.env` 设置以下两项，再启动网关。

```dotenv
TELEGRAM_LOCAL_API_URL=http://127.0.0.1:8081
TELEGRAM_MAX_FILE_BYTES=1073741824
```

4. 核对运行进程启动时间晚于变更文件时间，确认所有机器人入口都通过本地 `getMe`。
5. 在发起请求的同一机器人和聊天中发送原视频，确认 Telegram 返回成功的 Message，
   并检查 `file_size` 与原文件一致。单元测试通过不代表视频已送达。

本地上传通过 `sendVideo`/`sendDocument` 的 `file:///` URI 参数提交，不加载整个文件到网关内存。
直接提交裸绝对路径会被服务端解释为远程文件标识，不能代替文件 URI。
等待上传的超时为 3600 秒。网络失败后不自动重新上传，避免重复消息。
入站 `getFile` 返回绝对路径时，网关按块复制，并维持原有大小限制、私有权限与原子替换。

## 回退

先等待空闲并停止网关，在本地服务对所有机器人调用 `logOut`，移除
`TELEGRAM_LOCAL_API_URL`，将 `TELEGRAM_MAX_FILE_BYTES` 恢复为 `47185920`，然后启动网关。
云端 `logOut` 后 Telegram 有重新登录等待期；迁移失败时不能承诺立即切回云端。
停止本地容器可用 `docker compose -f docker/local-bot-api/compose.yaml down`，保留状态目录。
不要在迁移期间自动重放 CLI 回合。
