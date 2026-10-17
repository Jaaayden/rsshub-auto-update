# RSSHub Auto Update

为现有 RSSHub **整套 Docker Compose 项目**安装每日自动更新：RSSHub、Redis、Browserless，以及该项目默认启用的其他服务一同检查。失败时通过 Bark 推送，尝试恢复更新前的镜像。

## 一行安装

适用于 Linux + systemd、Python 3.9+、Docker Compose v2+（支持 `--wait-timeout` 和 `config --hash`），需已安装 `curl`、`flock`。在服务器执行：

```bash
curl -fsSL https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/main/install.sh | sudo bash
```

root 用户可以去掉 `sudo`。首次安装会从终端**隐藏输入 Bark HTTPS 地址**；地址不会回显。安装后每天北京时间 **04:00** 起执行（为避免多台服务器同时拉取镜像，最多随机延后 5 分钟），不立即更新、不发送测试通知、不改变服务器时区，关机错过的任务不会补跑。定时任务使用运行安装器的那个 Python 解释器（记录其绝对路径），不再依赖 `/usr/bin/python3`。

自动检测只有一个 RSSHub Compose 项目时直接安装；有多个候选或无法识别时退出，按提示指定项目和配置文件。每个服务必须有一个健康运行的容器，RSSHub、Redis、Browserless 必须配置健康检查。安装器检查当前 Compose/.env 能否还原容器环境；环境变量缺失或不同会列出字段名并停止，不显示值。配置哈希不同只作提示，无需为此重建容器；后续更新以当前 Compose 文件为准。

安装器从 GitHub 解析 `main` 的 commit，将程序和 SHA256 清单固定到同一 commit 下载并校验。校验用于发现损坏或混合下载，不是独立发布者签名；信任边界是本仓库与 GitHub HTTPS。需要固定版本时设置环境变量 `RSSHUB_AUTO_UPDATE_REF`（分支、标签或完整 commit），例如 `curl -fsSL …/install.sh | sudo RSSHUB_AUTO_UPDATE_REF=<完整 commit> bash`（变量须写在 `sudo` 之后，由 `bash` 一侧读取）。GitHub API 对未认证请求有每小时 60 次的限流，限流时安装器会退回 `git ls-remote`（需要已安装 git）；两者都失败则退出并提示。

## 自定义与无人值守安装

先将 Bark 地址放入服务器 root 可读文件，例如 `/root/bark-url`，权限设为 `0600`。**不要把真实密钥写进 Git 仓库或命令行参数。**

```bash
curl -fsSL https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/main/install.sh | sudo bash -s -- \
  --project rsshub \
  --compose-file /root/rsshub/docker-compose.yml \
  --time 04:00 --timezone Asia/Shanghai \
  --bark-url-file /root/bark-url
```

`--compose-file` 可重复，顺序与原部署一致。显式指定文件时必须指定项目；工作目录采用第一个文件的目录。自动识别则保留容器标签中的原工作目录。标准 `.env`、Compose 服务 `env_file` 由 Compose 读取；不会继承安装终端里的任意环境变量。自定义 `--env-file`、profiles、远程 Docker context 及缩放服务不属于首版支持范围。

重复安装保留已有 Bark、时间、时区和清理设置（除非显式覆盖），更新程序和同名 timer，不创建重复任务。`--no-prune` 关闭成功更新后的旧镜像清理（见下文），`--prune` 重新开启。共用的 Python 程序升级会同时应用于所有已安装项目。

批量部署：先通过自己的秘密分发方式把 Bark 文件放到每台服务器，再执行：

```bash
for host in server-a server-b; do
  ssh "root@$host" 'curl -fsSL https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/main/install.sh | bash -s -- --bark-url-file /root/bark-url'
done
```

## 运行与维护

以下 `rsshub` 是 Compose 项目名：

```bash
sudo rsshub-auto-update run rsshub          # 经 systemd 手动更新
sudo rsshub-auto-update status rsshub       # timer / service 状态
sudo rsshub-auto-update logs rsshub         # 最近日志及失败通知日志
sudo rsshub-auto-update test-notify rsshub  # 显式发送测试通知
sudo rsshub-auto-update prune rsshub        # 列出可清理的历史遗留镜像（只列出，不删除）
sudo rsshub-auto-update prune rsshub --yes  # 确认后删除；更新进行中会拒绝
sudo rsshub-auto-update uninstall rsshub    # 移除该项目的自动更新任务（更新正在运行时拒绝，可重复执行）
```

配置：`/etc/rsshub-auto-update/项目名.json`，包含 Bark 密钥，权限 `0600`。不要贴出整个配置文件。卸载不删除业务容器、Compose 文件、Cookie、镜像或数据卷；更新正在运行时会拒绝卸载（强行停止会中断 `compose up` 且不回退），等待结束后重试即可。卸载可重复执行，用于清理上次中断留下的残留。最后一个项目卸载后移除共用程序及 service 模板。备份镜像标签不会被删除，需要时执行 `docker image ls 'rsshub-auto-update-backup/*'` 查看并用 `docker image rm` 清理。

## 更新与回退行为

1. 获取项目锁和已安装 Cookie Sync 的 `deployment.lock_file`；锁占用则安静跳过。Cookie Sync 存在但锁配置无法解析时停止。
2. 验证默认服务和健康状态，保存当前镜像 ID，并为旧镜像添加项目专属备份标签（Compose 服务名含大写等 Docker 不接受的字符时，标签使用规整后的名称并附短哈希）。
3. 拉取整套镜像（单次最长 20 分钟）；任一拉取失败不重建。全部镜像未变化时不重启。
4. 有变化时执行整项目 `up -d --pull never --wait --wait-timeout 300`，Compose 按依赖关系更新。固定标签或 digest 保持原意，不会自动改成 `latest`。
5. 重建失败时，用临时 Compose JSON 覆盖文件恢复全部服务原镜像，禁止拉取，再次等待健康检查。
6. 更新成功（或无新镜像）后，清理**上一轮备份标签所指、已被本轮替换**的旧镜像，避免每日更新堆积占满磁盘。只删除工具自己记录过的镜像 ID，且仅当该镜像已没有任何仓库标签时才删（例如 `latest` 被新镜像取代后）；固定版本标签（如 `rsshub:1.2`）仍指向的镜像会保留，仍被容器使用的镜像 Docker 也会拒绝删除。更新失败时不清理，候选延后到下一次成功。可用 `--no-prune` 关闭。
7. 同一时刻再做一次**历史遗留镜像扫描**，兜住第 6 步跟踪不到的情况（装上本工具之前堆积的旧镜像、记录丢失等）。扫描只处理同时满足以下条件的镜像：没有任何仓库标签、没有任何容器（含已停止的）使用、不是当前运行或回退目标，并且启动配置（工作目录、入口命令、默认命令）与本项目正在运行的某个镜像相同——也就是“本项目服务的旧版本”。其他程序的镜像、带标签的镜像、被容器使用的镜像都不会被列入。

仅支持镜像部署，不执行本地构建，不启用额外 profiles，不操作其他项目，不执行 `docker image prune`、`docker system prune` 等全局清理。最近一份旧镜像通过 `rsshub-auto-update-backup/项目/服务:previous` 标签保留，手工全量清理仍可能删除它们。

**回退只恢复镜像，不恢复卷内数据。** Redis 版本变化或其他数据迁移可能无法通过旧镜像恢复。请自行维护数据备份；回退失败时先查看日志与容器状态，根据数据兼容性决定恢复备份或升级配置，不要清空数据卷。

最近镜像记录位于 `/var/lib/rsshub-auto-update/项目名/previous-images.json`，回退覆盖文件为同目录 `rollback.json`。人工恢复可用原来的 `docker compose -p 项目 -f 原文件 ...`，最后追加 `-f /var/lib/rsshub-auto-update/项目名/rollback.json up -d --pull never --wait`。成功回退后，下一次任务仍会尝试获取上游镜像；若上游版本持续故障，可先 `systemctl stop rsshub-auto-update@项目名.timer`。

## Bark 失败通知

更新前检查、拉取、更新、回退失败以及 systemd 超时/异常退出均通知。更新失败但回退成功也通知。成功、无更新、锁占用不通知。每次运行汇总一条，包括主机、项目、阶段、UTC 时间、回退结果及日志命令，不包含原始错误或环境变量。

请求使用 HTTPS POST JSON、证书校验，不跟随重定向；每次超时 10 秒，失败最多重试两次（间隔 5 秒、20 秒）。网络响应丢失后的重试可能重复送达。systemd `OnFailure` 使用运行标识去重；发送前标记通知已认领，避免脚本和兜底单元重复发送。若全部重试都失败，认领会被释放，由 `OnFailure` 兜底单元再尝试一轮。极端情况下在认领后进程被杀，会漏报。通知标记只保留最近 20 个。

如果更新因项目锁或 Cookie Sync 锁被占用而连续跳过 3 次，会发送一条“警告”通知（之后每多跳过 7 次再提醒一次），下次成功获取锁时计数清零。单次跳过仍保持安静。

服务总超时 45 分钟（`failure` 兜底单元 5 分钟）。异常强制终止时不能保证自动回退完成，需检查实际服务状态。命令失败时，退出码和 stderr 末尾 10 行（已去除 URL 凭据）会写入 `journalctl`，供本机排查；这些内容不会发往 Bark。Bark 自身失败仅记录脱敏日志，不递归告警、不改变更新结果。断电、完全断网或 Bark 不可用时，本机通知不能保证送达；本工具不是外部可用性监控。

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
shellcheck install.sh tests/integration.sh
bash -n install.sh tests/integration.sh
ruff check --select E9,F updater.py tests
bash tests/integration.sh # 需要 Docker；仅操作随机命名的隔离测试项目
```

GitHub Actions 执行上述检查（单元测试覆盖 Python 3.9 与 3.13）和整套更新/健康失败回退集成测试。Bark 使用模拟响应，不向真实设备发消息。修改 `updater.py` 后须重新生成 `SHA256SUMS`：

```bash
sha256sum updater.py > SHA256SUMS   # macOS: shasum -a 256 updater.py > SHA256SUMS
```

MIT License。
