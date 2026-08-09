"""文档解析 Demo（阶段 1，独立验证，未集成到本体）。

用法：
    uv run python demos/parse_demo.py <文件.pdf|.docx|.doc|.md|.txt>

- PDF / DOCX：MinerU（首次运行自动下载模型，约 1-2GB，仅一次）
- DOC：LibreOffice headless 转 DOCX 后走 MinerU
- MD / TXT：直接解码（utf-8/gbk）

输出：控制台预览前 2000 字符 + 完整文本写入 demos/out/<原名>.md
"""
import subprocess
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "out"
TMP_DIR = Path(__file__).resolve().parent / "tmp"
PREVIEW_CHARS = 2000

SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


def find_soffice() -> str | None:
    for cand in SOFFICE_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def extract_md_txt(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"无法解码 {path}（非 utf-8/gbk）")


def extract_with_mineru(path: Path) -> str:
    """MinerU CLI：解析到 TMP_DIR，返回生成的 markdown 文本。"""
    out_dir = TMP_DIR / "mineru"
    result = subprocess.run(
        ["mineru", "-p", str(path), "-o", str(out_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"MinerU 解析失败：\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    md_files = sorted(out_dir.rglob("*.md"))
    if not md_files:
        raise RuntimeError(f"MinerU 未产出 markdown（输出目录 {out_dir}）")
    return md_files[-1].read_text(encoding="utf-8", errors="replace")


def convert_doc_to_docx(path: Path, soffice: str) -> Path:
    """LibreOffice headless 转 docx，返回转换产物路径。"""
    conv_dir = TMP_DIR / "conv"
    conv_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [soffice, "--headless", "--convert-to", "docx", "--outdir", str(conv_dir), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice 转换失败：{result.stdout[-1000:]}{result.stderr[-1000:]}")
    out = conv_dir / f"{path.stem}.docx"
    if not out.exists():
        raise RuntimeError(f"LibreOffice 未产出 docx（目录 {conv_dir}）")
    return out


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：uv run python demos/parse_demo.py <文件>")
        sys.exit(1)
    path = Path(sys.argv[1]).resolve()
    if not path.is_file():
        print(f"文件不存在：{path}")
        sys.exit(1)

    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        text = extract_md_txt(path)
    elif suffix in (".pdf", ".docx"):
        text = extract_with_mineru(path)
    elif suffix == ".doc":
        soffice = find_soffice()
        if soffice is None:
            print("未找到 LibreOffice，请先安装：winget install TheDocumentFoundation.LibreOffice")
            sys.exit(1)
        docx = convert_doc_to_docx(path, soffice)
        text = extract_with_mineru(docx)
    else:
        print(f"不支持的扩展名：{suffix}（支持 .pdf/.docx/.doc/.md/.txt）")
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUT_DIR / f"{path.stem}.md"
    out_file.write_text(text, encoding="utf-8")
    print(f"解析完成：{len(text)} 字符")
    print(f"完整结果：{out_file}")
    print(f"--- 预览（前 {PREVIEW_CHARS} 字符） ---")
    print(text[:PREVIEW_CHARS])


if __name__ == "__main__":
    main()
