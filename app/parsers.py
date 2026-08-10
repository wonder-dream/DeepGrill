"""文档解析：PDF/DOCX 走 MinerU（CLI subprocess），DOC 走 LibreOffice 转换，MD/TXT 直接解码。

从 demos/parse_demo.py 提炼（demo 验证通过）；MinerU 模型首次解析自动下载（~1.2GB）。
懒加载 + CLI 调用：不拖慢启动、对 MinerU API 漂移免疫。
"""
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".doc", ".md", ".txt"}
TEXT_SUFFIXES = {".md", ".txt"}
MAX_TEXT_CHARS = 500_000  # 解析文本上限保护（防止超大文件撑爆入库/LLM 调用）

SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
)


def find_soffice() -> str | None:
    for cand in SOFFICE_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def _decode_md_txt(data: bytes) -> str:
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法解码文件内容（非 utf-8/gbk）")


def _extract_with_mineru(path: Path) -> str:
    """MinerU CLI 解析到临时目录，返回生成的 markdown 文本。"""
    with tempfile.TemporaryDirectory(prefix="mineru_parse_") as tmp:
        out_dir = Path(tmp) / "out"
        result = subprocess.run(
            ["mineru", "-p", str(path), "-o", str(out_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1200,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"MinerU 解析失败：{result.stdout[-1000:]}{result.stderr[-1000:]}"
            )
        md_files = sorted(out_dir.rglob("*.md"))
        if not md_files:
            raise RuntimeError("MinerU 未产出 markdown 结果")
        return md_files[-1].read_text(encoding="utf-8", errors="replace")


def _convert_doc_to_docx(path: Path, soffice: str, out_dir: Path) -> Path:
    """LibreOffice 转 docx；输出目录由调用方管理（TemporaryDirectory，用完即清）。"""
    result = subprocess.run(
        [soffice, "--headless", "--convert-to", "docx", "--outdir", str(out_dir), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"LibreOffice 转换失败：{result.stdout[-1000:]}{result.stderr[-1000:]}"
        )
    out = out_dir / f"{path.stem}.docx"
    if not out.exists():
        raise RuntimeError("LibreOffice 未产出 docx")
    return out


def extract_text(filename: str, data: bytes) -> str:
    """按扩展名提取文档文本（统一入口）。不支持的后缀抛 ValueError。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"不支持的文件类型：{suffix}（支持 {'/'.join(sorted(SUPPORTED_SUFFIXES))}）"
        )
    if suffix in TEXT_SUFFIXES:
        text = _decode_md_txt(data)
    elif suffix == ".doc":
        soffice = find_soffice()
        if soffice is None:
            raise RuntimeError(
                "未检测到 LibreOffice（DOC 文件需要），请安装：winget install TheDocumentFoundation.LibreOffice"
            )
        with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as f:
            f.write(data)
            tmp_doc = Path(f.name)
        try:
            with tempfile.TemporaryDirectory(prefix="libreoffice_conv_") as tmp:
                docx = _convert_doc_to_docx(tmp_doc, soffice, Path(tmp))
                text = _extract_with_mineru(docx)
        finally:
            tmp_doc.unlink(missing_ok=True)
    else:  # pdf / docx
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            tmp_file = Path(f.name)
        try:
            text = _extract_with_mineru(tmp_file)
        finally:
            tmp_file.unlink(missing_ok=True)
    if len(text) > MAX_TEXT_CHARS:
        logger.warning("解析文本超长截断：%s -> %d 字符", filename, len(text))
        text = text[:MAX_TEXT_CHARS]
    return text
