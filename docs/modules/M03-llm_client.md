# M3 LLM 客户端模块

> 路径：`app/llm/llm_client.py` ｜ 规模：~180 行 ｜ 依赖：openai SDK

## 1. 职责

统一 OpenAI 兼容调用（DeepSeek/Qwen/OpenAI 可换），封装重试/超时/JSON 输出解析；全项目唯一的 LLM 出入口。

## 2. 接口

```python
class LLMClient:
    def __init__(self, model: str, base_url: str, api_key: str, timeout: float = 60): ...

    def complete(self, messages: list[dict], json_schema: dict | None = None,
                 timeout: float | None = None) -> str | dict:
        """无 schema 返回文本；带 schema 返回解析后的 dict"""
```

**FakeLLM（`tests/fakes.py`，全项目测试复用）**

```python
class FakeLLM:
    """预录响应队列：每次调用弹出下一条；可注入异常序列模拟失败"""
    def __init__(self, responses: list[str | dict | Exception]): ...
```

## 3. 关键决策

- **重试策略**：指数退避 2 次（HTTP 5xx / 超时 / 连接错误），间隔 1s → 2s；最终失败抛 `LLMError`（retryable）
- **JSON 解析容错**：剥离 ```json 代码块围栏后再 `json.loads`，避免模型包 JSON 的常见失败
- **结构化输出**：带 schema 时请求 `response_format={"type": "json_object"}`，解析失败抛 `LLMJsonError`（retryable）
- **超时**：默认 60s；judge 用深思考模型时由调用方传 `timeout` 放宽
- 模型/端点全部来自 M1 配置，更换 provider 零代码改动

## 4. 错误隔离

- `LLMError`：网络/HTTP/超时/连接（retryable，调用方决定是否降级）
- `LLMJsonError`：输出不可解析（retryable）
- 重试耗尽后错误上抛，**由调用方决定降级策略**（判分失败跳过该题、追问降级为固定提示、生成失败记日志）

## 5. 测试计划（`tests/test_llm_client.py`）

| 类别 | 用例 |
|---|---|
| happy | 正常 JSON 返回、纯文本返回、带围栏 JSON 解析成功 |
| edge | 空内容、超长内容、schema 请求时模型返回纯文本（走失败路径） |
| fail | HTTP 5xx 重试 2 次后抛 LLMError（断言退避间隔序列 1s/2s）、JSON 损坏抛 LLMJsonError、401 无效 key、连接超时 |

## 6. 技术选型

**openai SDK**（OpenAI 兼容端点通用）

- 优：重试/超时/流式/错误类型成熟；一行换 provider（DeepSeek/通义/OpenAI 同协议）；自带结构化输出参数
- 缺：依赖较重（内含 httpx）；版本升级可能引入 API 变动
- 理由：手写 HTTP + SSE 是造轮子；多 provider 可配置（D5）是硬需求，官方 SDK 是最稳的兼容层

## 7. 实现提示

- 测试：构造底层 httpx 层用 `MockTransport` 模拟响应/异常；FakeLLM 不做网络，纯预录
- 重试间隔序列可注入（`backoff_func`），测试断言退避行为
- `FakeLLM` 的接口签名与 `LLMClient` 保持一致（鸭子类型），生产代码无需分支
