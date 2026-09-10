# RSSHub Auto Update

为现有 RSSHub **整套 Docker Compose 项目**安装每日自动更新：RSSHub、Redis、Browserless，以及该项目默认启用的其他服务一同检查。失败时通过 Bark 推送，尝试恢复更新前的镜像。

## 一行安装

适用于 Linux + systemd、Python 3.9+、Docker Compose v2+（支持 `--wait-timeout` 和 `config --hash`），需已安装 `curl`、`flock`。在服务器执行：

```bash
curl -fsSL https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/main/install.sh | sudo bash
```

root 用户可以去掉 `sudo`。首次安装会从终端**隐藏输入 Bark HTTPS 地址**；地址不会回显。安装后每天北京时间 **04:00** 执行，不立即更新、不发送测试通知、不改变服务器时区，关机错过的任务不会补跑。

自动检测只有一个 RSSHub Compose 项目时直接安装；有多个候选或无法识别时退出，按提示指定项目和配置文件。每个服务必须有一个健康运行的容器，RSSHub、Redis、Browserless 必须配置健康检查。安装前请用原 Compose 文件完成正常部署，安装器会比较配置哈希，拒绝无法复现的启动环境。

安装器从 GitHub 解析 `main` 的 commit，将程序和 SHA256 清单固定到同一 commit 下载并校验。校验用于发现损坏或混合下载，不是独立发布者签名；信任边界是本仓库与 GitHub HTTPS。

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

重复安装保留已有 Bark、时间、时区（除非显式覆盖），更新程序和同名 timer，不创建重复任务。共用的 Python 程序升级会同时应用于所有已安装项目。

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
sudo rsshub-auto-update uninstall rsshub    # 移除该项目的自动更新任务
```

配置：`/etc/rsshub-auto-update/项目名.json`，包含 Bark 密钥，权限 `0600`。不要贴出整个配置文件。卸载不删除业务容器、Compose 文件、Cookie、镜像或数据卷；最后一个项目卸载后移除共用程序及 service 模板。

## 更新与回退行为

1. 获取项目锁和已安装 Cookie Sync 的 `deployment.lock_file`；锁占用则安静跳过。Cookie Sync 存在但锁配置无法解析时停止。
2. 验证默认服务和健康状态，保存当前镜像 ID，并为旧镜像添加项目专属备份标签。
3. 拉取整套镜像；任一拉取失败不重建。全部镜像未变化时不重启。
4. 有变化时执行整项目 `up -d --pull never --wait --wait-timeout 300`，Compose 按依赖关系更新。固定标签或 digest 保持原意，不会自动改成 `latest`。
5. 重建失败时，用临时 Compose JSON 覆盖文件恢复全部服务原镜像，禁止拉取，再次等待健康检查。

仅支持镜像部署，不执行本地构建，不启用额外 profiles，不操作其他项目，不执行全局 prune。旧镜像通过 `rsshub-auto-update-backup/项目/服务:previous` 标签保留，手工全量清理仍可能删除它们。

**回退只恢复镜像，不恢复卷内数据。** Redis 版本变化或其他数据迁移可能无法通过旧镜像恢复。请自行维护数据备份；回退失败时先查看日志与容器状态，根据数据兼容性决定恢复备份或升级配置，不要清空数据卷。

最近镜像记录位于 `/var/lib/rsshub-auto-update/项目名/previous-images.json`，回退覆盖文件为同目录 `rollback.json`。人工恢复可用原来的 `docker compose -p 项目 -f 原文件 ...`，最后追加 `-f /var/lib/rsshub-auto-update/项目名/rollback.json up -d --pull never --wait`。成功回退后，下一次任务仍会尝试获取上游镜像；若上游版本持续故障，可先 `systemctl stop rsshub-auto-update@项目名.timer`。

## Bark 失败通知

更新前检查、拉取、更新、回退失败以及 systemd 超时/异常退出均通知。更新失败但回退成功也通知。成功、无更新、锁占用不通知。每次运行汇总一条，包括主机、项目、阶段、UTC 时间、回退结果及日志命令，不包含原始错误或环境变量。

请求使用 HTTPS POST JSON、证书校验，不跟随重定向；每次超时 10 秒，失败最多重试两次。网络响应丢失后的重试可能重复送达。systemd `OnFailure` 使用运行标识去重；发送前标记通知已认领，避免脚本和兜底单元重复发送。极端情况下在认领后进程被杀，会漏报。

服务总超时 30 分钟。异常强制终止时不能保证自动回退完成，需检查实际服务状态。Bark 自身失败仅记录脱敏日志，不递归告警、不改变更新结果。断电、完全断网或 Bark 不可用时，本机通知不能保证送达；本工具不是外部可用性监控。

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
shellcheck install.sh tests/integration.sh
bash -n install.sh tests/integration.sh
bash tests/integration.sh # 需要 Docker；仅操作随机命名的隔离测试项目
```

GitHub Actions 执行上述检查和整套更新/健康失败回退集成测试。Bark 使用模拟响应，不向真实设备发消息。修改 `updater.py` 后须重新生成 `SHA256SUMS`：

```bash
sha256sum updater.py > SHA256SUMS
```

MIT License。
