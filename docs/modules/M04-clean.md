# M4 清洗模块

> 路径：`app/pipeline/clean.py` ｜ 规模：~150 行 ｜ 依赖：BeautifulSoup4

## 1. 职责

将原始文本（HTML/口语化面经/简历）规范化为干净的纯文本：去 HTML 标签、广告水印、空白噪音，按"一面/二面"结构拆分。

## 2. 接口（纯函数，无 IO）

```python
def clean_text(raw: str) -> str
    """去 HTML 标签/广告/水印/空白噪音，返回规范化文本"""

def split_rounds(text: str) -> list[RoundText]
    """按一面/二面/三面结构拆分"""

class RoundText(TypedDict):
    name: str      # "一面" / "二面" / ...
    content: str
```

## 3. 关键决策

- **纯函数设计**：无文件/网络依赖，测试零成本，可直接给 M5/M6/M7 复用
- **广告/水印过滤**：关键词表做模块常量（"求私聊"、"vx"、"关注公众号"、"wx：xxx" 等），便于维护扩充
- **容错优先**：损坏 HTML 不抛异常，能提多少提多少；`<script>/<style>` 内容一律剥离
- 输入非 str（None/bytes）由调用方校验，本模块不防御（接口契约）
- `split_rounds` 识别 "一面|二面|三面|一面二面" 等模式，无轮次结构时返回 `[{name:"", content: text}]`

## 4. 错误隔离

- 纯函数，输入为 str 时不抛业务异常；解析失败的最坏结果是返回部分文本
- 不捕获内部异常（BeautifulSoup 对损坏 HTML 有容错，无需额外处理）

## 5. 测试计划（`tests/test_clean.py`）

| 类别 | 用例 |
|---|---|
| happy | 标准面经 HTML → 纯文本；广告行被滤除；三面结构正确拆分 |
| edge | 空串、全空白、超长文、中英文混合标点 |
| fail | 损坏 HTML（未闭合标签）不抛异常、`<script>` 内容剥离、HTML 实体（&amp;）正确解码、嵌入属性残留 |

## 6. 技术选型

**BeautifulSoup4**（html.parser 后端，不装 lxml）

- 优：容错解析损坏 HTML、`get_text()` 一行提取、后端纯 Python 无编译依赖
- 缺：多一个依赖；解析速度一般（个人工具量级无感）
- 理由：牛客正文是嵌套复杂 HTML，正则不可靠；标准库 `html.parser` 手写遍历繁琐且容错差

## 7. 实现提示

- 广告关键词表放模块常量 `AD_KEYWORDS`，测试直接引用断言
- `clean_text` 内部顺序：去 script/style → get_text → 实体解码（bs4 自动）→ 去广告行 → 压缩空白 → 去水印行
- 测试 fixture 放 `tests/fixtures/`，HTML 样本覆盖正常/损坏/带广告三类
