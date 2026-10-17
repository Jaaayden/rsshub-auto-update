# RSSHub Auto Update

给已经用 Docker Compose 部署的 RSSHub 加上**每日自动更新**：有新版本就更新，更新失败自动换回旧版本，并推送 Bark 通知你。

## 它会做什么

- 每天凌晨（默认北京时间 04:00）检查 RSSHub，以及同一个 Compose 项目里的 Redis、Browserless 等服务有没有新镜像。
- 有新镜像就**整套一起更新**，并等待健康检查通过；没有新镜像则什么都不做，不重启。
- 更新失败时自动换回更新前的镜像，同时通过 Bark 通知你。更新成功不打扰你。
- 自动清理被替换掉的旧镜像，磁盘不会越用越满。

## 安装前确认

- 一台 Linux 服务器（使用 systemd），已安装 Docker 和 Docker Compose v2，并有 root 权限。
- RSSHub 已经用 Compose 跑起来了，并且 rsshub、redis、browserless 都配置了健康检查（官方的 `docker-compose.yml` 示例已包含）。
- 一个 [Bark](https://bark.day.app) 推送地址（形如 `https://api.day.app/你的密钥`）。

## 安装

在服务器上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/main/install.sh | sudo bash
```

安装过程中会让你输入 Bark 地址，输入时不会显示。装好后不会立刻更新，也不会发送通知，下一个凌晨才会第一次运行。

脚本会自动找到你的 RSSHub 项目。如果找不到，或者服务器上有多个，它会提示你补充项目名和配置文件路径，按提示再运行一次即可。

## 日常使用

下面的 `rsshub` 是你的 Compose 项目名。

| 我想…… | 命令 |
| --- | --- |
| 现在就更新一次 | `sudo rsshub-auto-update run rsshub` |
| 看定时任务的状态 | `sudo rsshub-auto-update status rsshub` |
| 看最近的运行日志 | `sudo rsshub-auto-update logs rsshub` |
| 确认 Bark 能收到通知 | `sudo rsshub-auto-update test-notify rsshub` |
| 看有哪些旧镜像可以清理 | `sudo rsshub-auto-update prune rsshub`（只列出，不删除） |
| 确认清理旧镜像 | `sudo rsshub-auto-update prune rsshub --yes` |
| 卸载自动更新 | `sudo rsshub-auto-update uninstall rsshub` |

卸载只移除自动更新任务，不会删除你的容器、配置和数据。

## 常见问题

**收到“更新失败”的通知后怎么办？**
通知里会写明失败阶段和回退结果。在服务器上执行 `sudo rsshub-auto-update logs rsshub` 查看详情。

- 回退成功：服务仍然在旧版本上正常运行，不必着急。明天会再尝试更新，如果上游版本一直有问题，可以先停掉定时任务：`sudo systemctl stop rsshub-auto-update@rsshub.timer`。
- 回退失败：先用 `docker compose ps` 看看容器状态。手动恢复的步骤见[技术文档](docs/technical.md#手动恢复)。

**怎么修改更新时间、时区或 Bark 地址？**
重新运行安装命令并加上要改的参数，其他设置会保留：

```bash
# 改成每天 05:30（上海时间）更新
curl -fsSL https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/main/install.sh | sudo bash -s -- --time 05:30 --timezone Asia/Shanghai

# 换 Bark 地址：先把新地址写进只有 root 能读的文件，再指定它
curl -fsSL https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/main/install.sh | sudo bash -s -- --bark-url-file /root/bark-url
```

**需要手动清理镜像吗？**
一般不需要，每次更新成功后会自动清理。如果你是在很久以前就用旧版本，服务器上可能堆积了历史镜像，可以用上面的 `prune` 命令先查看、再确认清理。它只会处理没有标签、没有容器使用、并且和你的 RSSHub 项目属于同一类程序的旧镜像。

**可以一次给多台服务器安装吗？**
可以，见[技术文档](docs/technical.md#无人值守与批量部署)。

## 请注意

- **回退只恢复镜像，不恢复数据。** 如果新版本修改了 Redis 等的数据格式，旧镜像可能无法使用。请自行做好数据备份。
- 只支持用现成镜像部署的 Compose 项目，不支持本地构建镜像（`build:`）。
- 这是更新工具，不是监控工具。服务器断电、断网，或者 Bark 本身不可用时，收不到通知。

## 更多文档

[技术文档](docs/technical.md)：详细的更新与回退流程、通知机制、命令行参数、手动恢复、文件位置，以及开发与测试方法。

MIT License。
