# 部署（境外单机 · systemd）

> 决策与硬要求的权威在 **ADR-0008**，本文件只写**怎么做**。范围上它属于
> 「部署与基础设施」（`docs/v2范围基线.md`），落地形态由 **决策 73** 定。

目标机器：**美东 2C2G 单机**（因无法备案而选境外）。三个进程：web、worker、
两个定时器。**不用 Docker、不用编排** —— 一台机器上多一层容器只是多一处会忘记
持久化的地方。

```
/srv/deepgrill/
├── .venv/                    生产只装 .[dev] 之外的依赖
├── .env                      600，属主 deepgrill —— 密钥不进 git
├── data/interview.db         SQLite + WAL，唯一的写入目标
└── backups/                  `backup --dest` 的落地目录
```

## 一、前置条件（没做完这些不要上线）

| 前置 | 为什么 |
|---|---|
| **TLS 可用**（Cloudflare 做访客 TLS，源站只监听回环） | TLS 是**上线前置条件，不是待办**（决策 64）。明文生产 = 令牌与会话在公网上裸奔 |
| `DEEPGRILL_REQUIRE_SECURE_DB=true` | 迁移会种一个 `owner@local`，口令哈希是明文可读的 `PLACEHOLDER__…`。不替换就能被任何人拿到 owner 权限，所以**进程会拒绝启动**（决策 58） |
| `DEEPGRILL_SESSION_COOKIE_SECURE=true` | 本机 http 开发留 false，线上必须 true |
| `DEEPGRILL_TRUST_PROXY_HEADERS=true` | 跑在 Cloudflare 后面时，不信任转发头会让**所有请求看起来来自同一个 IP**，按 IP 的限流就此失效（应用还有一道按**账号**的，见决策 92）|
| 首件事是**替换 owner 口令** | `python -m app.cli set-password --email owner@local`（交互输入，或 `DEEPGRILL_NEW_PASSWORD='…'`；命令会同时撤销该账号的旧令牌）。**再用同一条命令改掉 `demo@local`** —— 它的口令 `deepgrill-demo` 明写在 `app/offline/seed.py` 里，是公开值 |
| **源站只允许 Cloudflare 回源** | 反代把访客 IP 取自 `CF-Connecting-IP`（见 nginx.conf 第 18–20 行的注释）。端口若对全网开放，任何人都能伪造这个头 → 按 IP 的限流形同不存在。用防火墙只放 CF 的出口网段 |
| **`/admin` 的白名单** | `nginx.conf` 里 `location ^~ /admin` 段留了 `deny all` + 一行注释掉的 `allow`。**上线前把你的出口 IP 填进去**（或改用 Cloudflare Access，那种做法下这一段可以删）|
| `mkdir -p backups` | systemd 的 `ReadWritePaths` 要求目录**存在**；不存在时 backup 单元直接起不来（`ProtectSystem=strict` 的经典坑）|

## 二、首次安装

```bash
# ① 用户与目录（backups 目录必须**先建**：ReadWritePaths 要求它存在）
sudo useradd --system --home /srv/deepgrill --shell /usr/sbin/nologin deepgrill
sudo mkdir -p /srv/deepgrill/backups && sudo chown -R deepgrill:deepgrill /srv/deepgrill

# ② 代码与虚拟环境（以 deepgrill 身份）
sudo -u deepgrill git clone <repo> /srv/deepgrill
cd /srv/deepgrill && sudo -u deepgrill python3 -m venv .venv
# ⚠️ **从锁文件装**，不要 `pip install -e .` 让它自己解析：
#    它会把 fastapi/starlette 解析成"当时最新"的版本，而那套组合没被测过
#    （`pyproject.toml` 只有下界 + 上界，真正可复现的那一份在 requirements.lock.txt）
sudo -u deepgrill .venv/bin/python -m pip install -r requirements.lock.txt
sudo -u deepgrill .venv/bin/python -m pip install -e . --no-deps

# ③ 配置（照 .env.example 抄，然后把上面那几个开关打开）
sudo -u deepgrill cp .env.example .env && sudo -u deepgrill chmod 600 .env

# ④ 建库 —— **迁移是显式的一步**（ADR-0008 / 决策 55）
sudo -u deepgrill .venv/bin/python -m migrations.run
sudo -u deepgrill .venv/bin/python -m migrations.run --check   # 再确认一次

# ⑤ 换掉两个公开口令（owner 的占位值、demo 的公开演示值）
sudo -u deepgrill .venv/bin/python -m app.cli set-password --email owner@local
sudo -u deepgrill .venv/bin/python -m app.cli set-password --email demo@local   # 或直接停用它

# ⑥ 装单元与反代
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deepgrill-web deepgrill-worker
sudo systemctl enable --now deepgrill-backup.timer deepgrill-maintenance.timer

sudo mkdir -p /etc/ssl/cloudflare
sudo cp <你的 Origin Certificate>.pem /etc/ssl/cloudflare/deepgrill.pem
sudo cp <你的 Origin Certificate>.key /etc/ssl/cloudflare/deepgrill.key
sudo chmod 600 /etc/ssl/cloudflare/deepgrill.key
sudo cp deploy/nginx.conf /etc/nginx/sites-available/deepgrill.conf
sudo sed -i 's/deepgrill.example.com/<你的域名>/g' /etc/nginx/sites-available/deepgrill.conf
# ⚠️ 再把 `location ^~ /admin` 段里那行注释掉的 allow 改成你自己的出口 IP
sudo nano /etc/nginx/sites-available/deepgrill.conf
sudo ln -sf /etc/nginx/sites-available/deepgrill.conf /etc/nginx/sites-enabled/deepgrill
sudo nginx -t && sudo systemctl reload nginx

# ⑦ 验证
curl -fsS http://127.0.0.1:8000/healthz          # 应用自身
curl -fsS https://<你的域名>/healthz             # 走完 CF + nginx 那一整条
systemctl status deepgrill-web deepgrill-worker --no-pager
systemctl list-timers 'deepgrill-*'
```

`/healthz` 只回一个 200 + JSON，**不查库**（限流也豁免它）—— 它是给外部探活用的，
不该因为一次写锁竞争就把整个站点判成挂了。

### 反代那一层为什么在仓库里（决策 77）

`app/llm/stt.py` 允许上传 **8MB** 的录音，而 nginx 默认 `client_max_body_size` 是
**1m** —— 不改这一行，超过 1MB 的语音回答在生产上会被挡成 **413**，而开发机上跑的
是 uvicorn，**这一层根本不存在，本地怎么测都测不出来**。`tests/test_deploy_units.py`
把这个数字与应用的常量对上了（还有 SSE 不许被缓冲、探活不许被限流、源站只许 TLS1.2+）。

TLS 的两段都要加密：访客 → Cloudflare 由 CF 负责；**Cloudflare → 源站这一段用
Origin Certificate**。"Flexible SSL"（CF→源站明文）会让令牌在公网上裸奔一段，
而这一段在链路上看起来像内网。

## 三、备份：做的那一半与**不做**的那一半

仓库里做的是「快照 + 清单 + **当场自验可恢复** + 保留策略」（`app/backup.py`）：

```bash
.venv/bin/python -m app.cli backup --dest ./backups --keep 7   # 快照并在当场验证
.venv/bin/python -m app.cli verify-backup ./backups/xxx.db.gz  # 只验证（演练用）
```

**异地那一跳**有两种拓扑，按"服务器够不够得着异地"选：

**① 服务器够得着（挂载上来的目录 / NAS / 另一块盘 / `rclone mount`）** ——
在 `.env` 里配一行，剩下的代码做：

```bash
DEEPGRILL_BACKUP_MIRROR=/mnt/offsite/deepgrill
```

`app.cli backup` 会把**已经自验过**的那一份复制过去，核对大小与 sha256，
并按保留份数清理。**那一步失败会让整个单元失败**（退出码包含异地那一步）——
"备份成功但没传出去"的绿色状态比没有备份更危险。

**② 服务器够不着你的本机（家用宽带大多如此）** —— **让本机定期来拉**：

```powershell
# 先手动跑一次确认能通（需要免密 SSH：把本机公钥加进服务端 deepgrill 的 authorized_keys）
pwsh -File deploy\pull-backups.ps1 -Server deepgrill@<你的服务器> -Dest D:\document\DeepGrill-back

# 挂到任务计划程序：每天 04:30（服务器 03:30 备份之后）
schtasks /create /tn "DeepGrill 拉备份" /sc daily /st 04:30 /tr ^
  "pwsh -NoProfile -File <仓库>\deploy\pull-backups.ps1 -Server deepgrill@<你的服务器>"
```

脚本会**只拉新增的**、复制完**在本机再核一遍 sha256**（不一致就删掉并非零退出：
"拉了一半的备份"比没有备份更危险）、并按份数保留最近若干份。

演练要求：**至少每季度真的恢复一次**（`verify-backup` 会解包、校验校验和、
跑 `PRAGMA integrity_check`、对表与行数、核对迁移版本）。没有演练过的备份不算备份。

## 四、升级与回滚

```bash
cd /srv/deepgrill
sudo -u deepgrill git pull
# 依赖变了才需要重装 —— 而"变了"的判据是 requirements.lock.txt：它一改就照锁文件装
sudo -u deepgrill .venv/bin/python -m pip install -r requirements.lock.txt
sudo -u deepgrill .venv/bin/python -m pip install -e . --no-deps
sudo -u deepgrill .venv/bin/python -m migrations.run      # ① 先迁移
sudo systemctl restart deepgrill-web deepgrill-worker      # ② 再换进程
```

顺序不能反：启动时自动迁移会把"回滚一次发布"变成"回滚一次数据"。

**回滚**：迁移是向前的，没有 `down`。回滚 = 用备份恢复（这就是为什么备份必须
**验证过能恢复**，而不是"生成成功"）。

## 五、日常

| 看什么 | 命令 |
|---|---|
| 服务与定时器状态 | `systemctl status deepgrill-web deepgrill-worker --no-pager` |
| 日志 | `journalctl -u deepgrill-worker -n 100 --no-pager` |
| 队列 / 待定池 / 库体积 / 质量 | 站点 `/admin/observability`（决策 23） |
| 库规模 + 冷启动顺序 | `.venv/bin/python -m app.cli status` |
| 阈值标定（只读） | `.venv/bin/python -m app.cli calibrate` |
