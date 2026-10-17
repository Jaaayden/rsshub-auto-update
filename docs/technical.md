# 技术文档

面向需要了解细节的用户和开发者。日常使用请看 [README](../README.md)。

目录：[安装器的行为](#安装器的行为) · [无人值守与批量部署](#无人值守与批量部署) · [更新与回退流程](#更新与回退流程) · [镜像清理](#镜像清理) · [Bark 通知](#bark-通知) · [超时与日志](#超时与日志) · [手动恢复](#手动恢复) · [文件位置](#文件位置) · [开发与测试](#开发与测试)

## 安装器的行为

**环境要求：** Linux + systemd、Python 3.9+、Docker Compose v2+（支持 `--wait-timeout` 和 `config --hash`），需已安装 `curl`、`flock`。

**项目识别：** 自动检测到只有一个 RSSHub Compose 项目时直接安装；有多个候选或无法识别时退出，按提示用 `--project` 和 `--compose-file` 指定。自动识别会保留容器标签中记录的原工作目录。

**安装前检查：**

- 每个默认启用的服务必须有且只有一个健康运行的容器；RSSHub、Redis、Browserless 必须配置健康检查。
- 安装器会用当前的 Compose 文件和 `.env` 还原容器环境，与正在运行的容器对比。环境变量缺失或不一致时列出字段名并停止（不显示值），避免日后更新时因为丢了变量而悄悄改变服务配置。
- 配置哈希（`config-hash`）不同只作提示，不会因此要求重建容器；后续更新以当前 Compose 文件为准。

**定时任务：** 每天指定时间（默认北京时间 04:00）起执行，最多随机延后 5 分钟，避免多台服务器同时拉取镜像。关机错过的任务不会补跑。安装后不会立即更新、不发送测试通知、不改变服务器时区。

**Python 解释器：** 定时任务使用运行安装器的那个 Python 解释器（记录其绝对路径），不依赖 `/usr/bin/python3`。

**下载与校验：** 安装器从 GitHub 解析分支（默认 `main`）当前的 commit，把 `updater.py` 和 `SHA256SUMS` 固定到同一 commit 下载并校验。这个校验用于发现下载损坏或混合下载，**不是独立的发布者签名**；信任边界是本仓库与 GitHub HTTPS。

**固定版本：** 设置环境变量 `RSSHUB_AUTO_UPDATE_REF`（分支、标签或完整 commit），例如：

```bash
curl -fsSL https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/main/install.sh | sudo RSSHUB_AUTO_UPDATE_REF=<完整 commit> bash
```

变量要写在 `sudo` 之后，由 `bash` 一侧读取。GitHub API 对未认证请求每小时限流 60 次，限流时安装器会退回 `git ls-remote`（需要已安装 git）；两者都失败则退出并提示。

**重复安装：** 保留已有的 Bark、时间、时区和清理设置（除非显式覆盖），更新程序和同名 timer，不创建重复任务。共用的 Python 程序升级会同时应用于该机器上所有已安装的项目。

**不支持的情况：** 本地构建（`build:`）、额外的 profiles、自定义 `--env-file`、远程 Docker context、缩放（多副本）的服务。标准 `.env` 和服务的 `env_file` 由 Compose 读取；不会继承安装终端里的其他环境变量。

## 无人值守与批量部署

先把 Bark 地址放进服务器上 root 可读的文件，例如 `/root/bark-url`，权限设为 `0600`。**不要把真实密钥写进 Git 仓库或命令行参数。**

```bash
curl -fsSL https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/main/install.sh | sudo bash -s -- \
  --project rsshub \
  --compose-file /root/rsshub/docker-compose.yml \
  --time 04:00 --timezone Asia/Shanghai \
  --bark-url-file /root/bark-url
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `--project` | Compose 项目名。显式指定配置文件时必须同时指定 |
| `--compose-file` | Compose 文件的绝对路径，可重复；顺序与原部署一致，工作目录取第一个文件所在目录 |
| `--time` / `--timezone` | 更新时间（`HH:MM`）和时区，默认 `04:00` / `Asia/Shanghai` |
| `--bark-url-file` | 保存 Bark 地址的文件；不提供时，首次安装会从终端隐藏输入 |
| `--prune` / `--no-prune` | 开启 / 关闭更新成功后的旧镜像清理，默认开启 |

批量部署：先用自己的方式把 Bark 文件分发到每台服务器，再执行：

```bash
for host in server-a server-b; do
  ssh "root@$host" 'curl -fsSL https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/main/install.sh | bash -s -- --bark-url-file /root/bark-url'
done
```

## 更新与回退流程

1. 获取项目锁，以及已安装的 Cookie Sync 的 `deployment.lock_file`；锁被占用则安静跳过。Cookie Sync 存在但锁配置无法解析时停止。
2. 验证默认服务和健康状态，保存当前镜像 ID，并为旧镜像添加项目专属的备份标签。Compose 服务名含大写等 Docker 不接受的字符时，标签会使用规整后的名称并附短哈希。
3. 拉取整套镜像（单次最长 20 分钟）；任一拉取失败则不重建。全部镜像都没变化时不重启。
4. 有变化时执行整个项目的 `up -d --pull never --wait --wait-timeout 300`，由 Compose 按依赖关系更新。固定标签或 digest 保持原意，不会被自动改成 `latest`。
5. 重建失败时，用一个临时的 Compose JSON 覆盖文件恢复所有服务的原镜像，禁止拉取，再次等待健康检查。
6. 更新成功（或无新镜像）后清理旧镜像，见下一节。

不执行本地构建，不启用额外 profiles，不操作其他项目。

**回退只恢复镜像，不恢复卷内数据。** Redis 版本变化或其他数据迁移可能无法通过旧镜像恢复。请自行备份数据；回退失败时先看日志和容器状态，根据数据兼容性决定是恢复备份还是调整配置，不要清空数据卷。成功回退后，下一次任务仍会尝试获取上游镜像。

## 镜像清理

每次更新成功（或检查后无新镜像）后做两件事，仅在开启清理（默认）时执行：

**1. 清理被替换的备份。** 每次运行前记下备份标签 `rsshub-auto-update-backup/项目/服务:previous` 原来指向的镜像；标签改指新的备份后，那份旧镜像就成了候选。只删除工具自己记录过的 ID，并且仅当该镜像已没有任何仓库标签时才删（例如 `latest` 已指向新镜像）。固定版本标签（如 `rsshub:1.2`）仍指向的镜像会保留；仍被容器使用的镜像 Docker 也会拒绝删除。更新失败时不清理，候选延后到下一次成功。

**2. 扫描历史遗留镜像。** 兜住第 1 步跟踪不到的情况（装上本工具之前堆积的旧镜像、记录丢失等）。只处理**同时满足**以下条件的镜像：

- 没有任何仓库标签；
- 没有任何容器（含已停止的）使用；
- 不是当前运行的镜像，也不是回退目标；
- 启动配置（工作目录、入口命令、默认命令）与本项目正在运行的某个镜像相同，即“本项目服务的旧版本”；
- 与某个在用镜像的层完全相同的无标签重复条目不处理（删了也释放不了空间）。

其他程序的镜像、带标签的镜像、被容器使用的镜像都不会被列入。工具**不会**运行 `docker image prune`、`docker system prune` 等全局清理。

手动运行同样的扫描：`rsshub-auto-update prune 项目名`（只列出）、`… --yes`（确认删除）。更新正在运行时会拒绝执行。

最近一份旧镜像通过备份标签保留，用于回退；手工全量清理（如 `docker system prune -a`）仍可能删除它们。

## Bark 通知

**何时通知：** 更新前检查、拉取、更新、回退失败，以及 systemd 超时或异常退出，都会通知。更新失败但回退成功也通知。成功、无更新、单次锁占用不通知。

**内容：** 每次运行汇总一条，包括主机、项目、阶段、UTC 时间、回退结果和查看日志的命令，不包含原始错误或环境变量。

**发送方式：** HTTPS POST JSON，校验证书，不跟随重定向；每次超时 10 秒，失败最多重试两次（间隔 5 秒、20 秒）。网络响应丢失后的重试可能造成重复送达。

**去重：** systemd 的 `OnFailure` 兜底单元用运行标识去重。发送前先标记“已认领”，避免脚本和兜底单元重复发送；如果所有重试都失败，认领会被释放，由兜底单元再尝试一轮。极端情况下认领后进程被杀，会漏报。通知标记只保留最近 20 个。

**连续跳过的警告：** 更新因项目锁或 Cookie Sync 锁被占用而连续跳过 3 次，会发一条“警告”通知（之后每多跳过 7 次再提醒一次），下次成功获取锁时计数清零。

**局限：** Bark 自身失败只记录脱敏日志，不递归告警、不改变更新结果。断电、完全断网或 Bark 不可用时，通知不能保证送达；本工具不是外部可用性监控。

## 超时与日志

- 服务总超时 45 分钟（失败通知的兜底单元 5 分钟）。异常强制终止时不能保证自动回退完成，需要检查实际服务状态。
- 命令失败时，退出码和 stderr 的最后 10 行（已去除 URL 中的凭据）会写入 `journalctl`，供本机排查；这些内容不会发往 Bark。
- 查看日志：`sudo rsshub-auto-update logs 项目名`，或 `journalctl -u rsshub-auto-update@项目名.service`。

## 手动恢复

回退覆盖文件和更新前的镜像记录都保存在 `/var/lib/rsshub-auto-update/项目名/`：

- `previous-images.json`：更新前各服务的镜像 ID。
- `rollback.json`：回退用的 Compose 覆盖文件。

用原来的 `docker compose` 命令，在最后追加覆盖文件即可恢复到旧镜像：

```bash
docker compose -p 项目名 -f /原来的/docker-compose.yml \
  -f /var/lib/rsshub-auto-update/项目名/rollback.json up -d --pull never --wait
```

如果上游版本持续有问题，可以先停掉定时任务：`systemctl stop rsshub-auto-update@项目名.timer`。

## 文件位置

| 路径 | 内容 |
| --- | --- |
| `/etc/rsshub-auto-update/项目名.json` | 项目配置，**包含 Bark 密钥**，权限 `0600`；不要贴出整个文件 |
| `/var/lib/rsshub-auto-update/项目名/` | 运行状态：`run.json`、`previous-images.json`、`rollback.json`、锁文件等 |
| `/usr/local/lib/rsshub-auto-update/updater.py` | 程序本体，所有项目共用 |
| `/usr/local/bin/rsshub-auto-update` | 命令行入口 |
| `/etc/systemd/system/rsshub-auto-update@.service` 等 | systemd 单元：更新服务、失败通知服务、每个项目的 timer |

**卸载：** 更新正在运行时会拒绝卸载（强行停止会中断 `compose up` 且不会回退），等它结束后重试即可。卸载可以重复执行，用来清理上次中断留下的残留。它不删除业务容器、Compose 文件、Cookie、镜像或数据卷；最后一个项目卸载后，共用的程序和 service 模板也会移除。备份镜像标签不会被删除，需要时用 `docker image ls 'rsshub-auto-update-backup/*'` 查看，再用 `docker image rm` 清理。

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
shellcheck install.sh tests/integration.sh
bash -n install.sh tests/integration.sh
ruff check --select E9,F updater.py tests
bash tests/integration.sh   # 需要 Docker；仅操作随机命名的隔离测试项目
```

GitHub Actions 会执行上述检查（单元测试覆盖 Python 3.9 与 3.13）和完整的更新 / 健康检查失败回退集成测试。Bark 使用模拟响应，不会向真实设备发消息。

修改 `updater.py` 后须重新生成 `SHA256SUMS`：

```bash
sha256sum updater.py > SHA256SUMS   # macOS: shasum -a 256 updater.py > SHA256SUMS
```
