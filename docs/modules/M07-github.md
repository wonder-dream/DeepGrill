# M7 GitHub 面经源模块

> 路径：`app/crawler/github.py` ｜ 规模：~200 行 ｜ 依赖：subprocess 调 git + `app/crawler/license.py`
> 更新日期：2026-08-08（实现偏差与修复记录）；2026-08-16（许可白名单与来源溯源）

## 1. 职责

按配置的仓库列表 `git pull`（首拉 clone），校验仓库 LICENSE（生产默认白名单），遍历 markdown 文件提取标题/正文，连同 `license/author/repo_url` 溯源字段清洗后入库。

## 2. 接口

```python
def collect(repos: list, cache_dir: Path, *,
            require_license: bool = False,
            allowed_licenses: list[str] | None = None) -> list[Source]
    """入口：本日增量采集；repo 可为字符串或 GitHubRepo 配置对象；无许可整仓跳过"""

def ensure_repos(repos: list[str], cache_dir: Path) -> list[Path]
    """clone（不存在）或 pull（已存在），返回仓库本地路径列表"""
```

## 3. 关键决策

- **单仓库隔离**：单仓库失败（网络/认证/冲突/无许可）跳过该仓库，不影响其他仓库与其他源
- **许可白名单（2026-08-16）**：`github_require_license=true`（默认）时，无 LICENSE 或不在 `allowed_licenses` 的仓库整仓跳过；`manual_license` 支持人工授权仓库
- **来源溯源（2026-08-16）**：每个 `Source` 写 `license`（SPDX）、`author`（owner）、`repo_url`
- **文件过滤**：跳过 README/非 markdown/代码目录（如 `code/`、`scripts/`）/二进制/超 1MB 文件
- **增量**：仓库已 pull 的最新内容哈希入库，UNIQUE 天然去重
- **仓库缓存目录**：放数据目录（如 `data/repos/`），gitignore 排除，不污染项目仓库
- 每个文件一个 `Source`（title=文件名或首行标题，url=仓库内相对路径拼接 raw 链接）

## 4. 错误隔离

- `GitHubError`（继承 CrawlerError）：git 命令失败、仓库不存在、认证失败
- 无 git 命令：启动时检测一次（`shutil.which("git")`），缺失则本源全部跳过并记日志
- 单个文件解析失败仅跳过该文件，不中断整个仓库

## 5. 测试计划（`tests/test_github.py`）

**真实 git 操作（临时 fixture 仓库）**

| 类别 | 用例 |
|---|---|
| happy | 临时目录 git init 造仓库 → clone 成功；新增文件 pull 后入库；删除文件不再产出 |
| edge | 空仓库；无 markdown 仓库；多仓库并行；大文件被跳过 |
| fail | 无 git 命令（跳过并记录）；仓库不存在；认证失败；网络错误（mock subprocess 返回值） |

## 6. 技术选型

**subprocess 调 git**（非 dulwich）

- 优：git 能力完整（clone/pull/认证/子模块）、零 Python 依赖、行为与用户 git 环境一致
- 缺：依赖系统安装 git（Windows 需 Git for Windows——个人开发机几乎必装）
- 理由：dulwich 纯 Python 但仅功能子集、维护成本高；为保真度与零维护选 subprocess

## 7. 实现提示

- subprocess 调用统一带 `timeout`（如 120s）防挂起；输出设 `encoding="utf-8", errors="replace"`
- 文件遍历用 `Path.rglob("*.md")`；目录排除名单（`code`, `scripts`, `assets`, `images`）做模块常量
- 测试中真实 git 操作放 `pytest.mark.skipif(shutil.which("git") is None)` 保护
- pull 前 `git rev-parse --verify HEAD` 判断是否已 clone；pull 失败不删除缓存（下次可重试）

## 8. 实现偏差与修复记录

**偏差**
- `CLONE_BASE` 为模块常量（默认 `https://github.com`），测试 monkeypatch 为本地 file:// 仓库根——离线测试硬要求
- clone URL 不带 `.git` 后缀（github.com 与本地路径均兼容）
- `_git` 捕获 `(subprocess.SubprocessError, OSError)` 统一包装为 `GitHubError`

**修复（2026-08-08）**
- **克隆目录嵌套 bug**：`_git(cache_dir, "clone", ..., str(repo_path))` 中 repo_path 为相对路径，git 相对自身 cwd 解析 → 双重嵌套（`data/repos/data/repos/...`），入库 0；测试未捕获因 tmp_path 为绝对路径。修复：`repo_path = (cache_dir / repo.replace("/", "__")).resolve()`；回归测试 `test_relative_cache_dir_no_nesting`（monkeypatch.chdir + 相对 cache_dir）
- 该 bug 导致首轮「立即更新」静默零入库；修复后真实采集 111 源成功
