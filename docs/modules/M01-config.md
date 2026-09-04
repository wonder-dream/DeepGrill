# M1 配置加载模块

> 路径：`app/config.py` ｜ 规模：~80 行 ｜ 依赖：pyyaml, pydantic, python-dotenv
> 更新日期：2026-08-07（实现偏差修复）

## 1. 职责

加载 `config.yaml` + 环境变量（API key / cookie 只从 env 读），输出 pydantic 校验后的配置对象，供全项目注入使用。

## 2. 接口

```python
def load_config(path: Path) -> AppConfig
```

`AppConfig` 结构：

```python
class AppConfig(BaseModel):
    llm: LLMConfig          # base_url, api_key_env, generate_model, judge_model
    nowcoder: NowcoderConfig  # cookie_env, request_interval, retries
    daily: DailyConfig      # max_new_questions, knowledge_limit, design_limit,
                            # project_limit, chain_max_rounds, schedule
    notification: NotificationConfig  # enabled
    sources: SourcesConfig  # github_repos: list[str]; nowcoder_enabled: bool(默认 False)
```

## 3. 关键决策

- **密钥不落盘**：`api_key_env` / `cookie_env` 只声明环境变量名；`load_config` 先加载 config.yaml 同目录的 `.env` 文件（python-dotenv，gitignore 排除），真实环境变量优先、`.env` 兜底；配置文件中不得出现明文密钥
- **严格校验**：必填字段缺失或类型错误直接抛 `ConfigError`，服务启动时一次性捕获并给出可读信息，不静默默认值
- **安全默认项**：仅对无风险字段提供默认值（如 `schedule="08:00"`、`notification.enabled=true`）
- **多余字段忽略**：yaml 中额外字段不影响加载（容忍未来兼容）

## 4. 错误隔离

- 全部配置错误归为 `ConfigError`（含 env 未设置的 `EnvVarMissing` 子类）
- 服务启动阶段捕获，未初始化成功则不启动其他模块——配置错误应"fail fast"

## 5. 测试计划（`tests/test_config.py`）

| 类别 | 用例 |
|---|---|
| happy | 完整 yaml + env 正常加载，字段映射正确 |
| edge | 可选字段缺省取默认值；yaml 含多余字段被忽略；`.env` 文件加载生效且真实环境变量优先 |
| fail | 文件不存在 / yaml 语法错误 / 类型错误（knowledge_limit 传字符串）/ 必填字段缺失 / api_key_env 对应 env 未设置（含空串）/ 非 UTF-8 编码（GBK） |

## 6. 技术选型

**pyyaml + pydantic + python-dotenv**

- 优：类型安全、错误信息清晰（pydantic 校验错误定位到字段）、与 SQLModel/FastAPI 同生态、简历加分
- 缺：三个第三方依赖（均轻量）
- 理由：配置结构随项目增长（多源、多限流参数），手写 dict 校验不可靠；pydantic 校验成本低收益高；dotenv 免手写 .env 解析

## 7. 实现提示

- `load_config` 内部先 `load_dotenv`（config.yaml 同目录 `.env`，不覆盖已有环境变量）→ 读 yaml → pydantic 校验 → 校验 env 声明的密钥已设置（未设置/空串抛 `EnvVarMissing`）
- `AppConfig` 与全部子模型均设为 `frozen`，防运行期误改（仅顶层 frozen 挡不住嵌套修改）
- 测试用 `tmp_path` 写临时 yaml + `monkeypatch.setenv`/`delenv`，.env 加载测试需先 delenv 隔离
