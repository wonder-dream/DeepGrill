# M5 面经/简历导入模块

> 路径：`app/crawler/importer.py` ｜ 规模：~150 行 ｜ 依赖：标准库

## 1. 职责

将本地文件（markdown/txt）导入为 `Source` 记录：面经（type=manual）与简历（type=resume）；简历导入后触发 M8 生成 project 题。

## 2. 接口

```python
def import_file(path: Path, source_type: str) -> Source
    """source_type ∈ manual|resume；重复导入幂等返回已有记录"""

def collect() -> list[Source]
    """与其他源同协议（M12 遍历用）：扫描配置的导入目录，返回全部源"""
```

CLI 入口：`python -m app import <file> [--type resume|manual]`

## 3. 关键决策

- **幂等**：内容哈希与库内比对，重复返回已有记录（`DuplicateSource` 标记）
- **编码处理**：优先 utf-8，失败回退 gbk，再失败抛 `ImportError`
- **清洗复用**：导入内容经 M4 `clean_text` 规范化后入库
- **简历联动**：type=resume 导入后调用 M8 `generate_project_questions` 生成 project 题（受 `daily.project_limit` 节流）
- 标题提取：markdown 首行 `# 标题` 或文件名

## 4. 错误隔离

- 文件问题（不存在/不可读/编码失败）抛 `ImportError`，不影响其他文件与源
- 重复导入不报错：返回已有记录 + 幂等标记，调用方可忽略
- project 题生成失败不影响简历源本身入库（生成失败仅记日志）

## 5. 测试计划（`tests/test_importer.py`）

| 类别 | 用例 |
|---|---|
| happy | md/txt 正常导入；重复导入幂等返回已存在；简历导入触发 project 题生成（FakeLLM） |
| edge | 空文件、超大文件、BOM 头、gbk 编码中文 |
| fail | 路径不存在、权限拒绝、非法 type 参数、编码双重失败 |

## 6. 技术选型

**纯标准库**（pathlib + codecs）

- 优：零依赖、行为可预测
- 缺：无
- 理由：无网络无 LLM 的最简模块，引入第三方无收益

## 7. 实现提示

- `collect()` 扫描配置的导入目录（如 `data/imports/`），文件名带前缀区分 resume/manual
- CLI 用 `argparse` 或 `fire` 均可；`python -m app` 入口在包 `__main__.py` 聚合所有 CLI
- 测试用 `tmp_path` 造临时文件，不碰真实目录
