from __future__ import annotations

import sys
import os
import re
import base64
import pickle
import traceback
import threading
import subprocess
from pathlib import Path
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Callable, List, Tuple, Dict, Set

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QComboBox, QTextEdit,
    QProgressBar, QGroupBox, QSpinBox, QMessageBox, QLineEdit,
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject
from PyQt5.QtGui import QFont

import fitz  # PyMuPDF
from PIL import Image


# ============================================================
# 常量与配置
# ============================================================

# 图片最小有效尺寸
MIN_IMAGE_DIMENSION = 50

# 布局解析中"同行图片"的垂直容差比例
SAME_ROW_Y_TOLERANCE = 0.03

# 图片宽度裁切范围
IMG_WIDTH_MIN = 0.1
IMG_WIDTH_MAX = 0.95
IMG_ROW_AVAILABLE_WIDTH = 0.92

# 图片居中判定阈值
IMAGE_CENTER_LEFT_THRESHOLD = 0.4
IMAGE_CENTER_RIGHT_THRESHOLD = 0.6

# 页面文件名模式
PAGE_IMAGE_PATTERN = re.compile(r'pdf_page_(\d+)\.png$')
RAW_RESULT_PATTERN = re.compile(r'page_(\d+)_raw\.pkl$')
TEX_PAGE_PATTERN = re.compile(r'page_(\d+)\.tex$')

# 默认 DPI
DEFAULT_DPI = 600


@dataclass
class PipelineConfig:
    """流水线配置，集中管理所有参数"""
    pdf_path: str = ""
    output_dir: str = ""
    mode: str = "render"         # "render" | "extract"
    dpi: int = DEFAULT_DPI
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    api_key: str = ""

    @property
    def pdf_pages_dir(self) -> str:
        return os.path.join(self.output_dir, "pdf_pages")

    @property
    def raw_results_dir(self) -> str:
        return os.path.join(self.output_dir, "raw_results")

    @property
    def extracted_images_dir(self) -> str:
        return os.path.join(self.output_dir, "images")

    @property
    def tex_pages_dir(self) -> str:
        return os.path.join(self.output_dir, "tex_pages")

    @property
    def output_tex_file(self) -> str:
        return os.path.join(self.output_dir, "output.tex")

    def validate(self, steps: List[int]) -> Optional[str]:
        """验证参数，返回错误信息或 None"""
        if not self.pdf_path:
            return "请先选择 PDF 文件！"
        if not os.path.exists(self.pdf_path):
            return f"PDF 文件不存在:\n{self.pdf_path}"
        if not self.output_dir:
            return "请先选择输出目录！"
        if 2 in steps and not self.api_key:
            return "执行 Step2 需要填写 API Key！"
        return None


class LogLevel(Enum):
    """日志级别"""
    INFO = auto()
    SUCCESS = auto()
    WARNING = auto()
    ERROR = auto()
    HEADER = auto()


# 日志级别对应的 HTML 颜色
LOG_COLORS: Dict[LogLevel, str] = {
    LogLevel.INFO:    "#d4d4d4",
    LogLevel.SUCCESS: "#4EC9B0",
    LogLevel.WARNING: "#DCDCAA",
    LogLevel.ERROR:   "#F44747",
    LogLevel.HEADER:  "#569CD6",
}


# LaTeX 模板
TEX_PREAMBLE = r"""\documentclass[12pt, a4paper]{article}
\usepackage[UTF8]{ctex}
\usepackage{amsmath, amssymb, amsfonts}
\usepackage{graphicx}
\usepackage{float}
\usepackage{pifont}
\usepackage{multirow}
\usepackage[margin=2cm]{geometry}
\setlength{\parindent}{0pt}

\begin{document}
"""

TEX_ENDING = r"""
\end{document}
"""


# ============================================================
# 通用工具函数
# ============================================================

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]
CancelCheck = Callable[[], bool]


def img_to_data_url(image_path: str) -> str:
    """将图片文件转为 data URL"""
    path = Path(image_path)
    b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
    suffix = path.suffix.lower().lstrip(".")
    mime_type = "jpeg" if suffix == "jpg" else suffix
    return f"data:image/{mime_type};base64,{b64}"


def _scan_files_by_pattern(
    directory: str, pattern: re.Pattern, suffix: str
) -> List[Tuple[int, str]]:
    """通用文件扫描: 按正则匹配文件名，返回 (编号, 路径) 列表"""
    results = []
    if not os.path.isdir(directory):
        return results
    for fname in os.listdir(directory):
        if not fname.endswith(suffix):
            continue
        m = pattern.match(fname)
        if m:
            num = int(m.group(1))
            results.append((num, os.path.join(directory, fname)))
    results.sort(key=lambda x: x[0])
    return results


def scan_page_images(pages_dir: str) -> List[Tuple[int, str]]:
    return _scan_files_by_pattern(pages_dir, PAGE_IMAGE_PATTERN, ".png")


def scan_completed_raw_results(raw_dir: str) -> Set[int]:
    """返回已完成 OCR 的页码集合 (0-based)"""
    items = _scan_files_by_pattern(raw_dir, RAW_RESULT_PATTERN, ".pkl")
    return {num - 1 for num, _ in items}


def scan_completed_tex_pages(tex_pages_dir: str) -> Set[int]:
    """返回已生成 TeX 的页码集合 (0-based)"""
    items = _scan_files_by_pattern(tex_pages_dir, TEX_PAGE_PATTERN, ".tex")
    return {num - 1 for num, _ in items}


def scan_tex_pages(tex_pages_dir: str) -> List[Tuple[int, str]]:
    return _scan_files_by_pattern(tex_pages_dir, TEX_PAGE_PATTERN, ".tex")


def save_raw_result(resp, page_num: int, output_dir: str) -> Tuple[str, str]:
    """保存 OCR 原始结果为 pkl + 可读 txt"""
    os.makedirs(output_dir, exist_ok=True)
    stem = f"page_{page_num + 1:04d}_raw"
    pkl_path = os.path.join(output_dir, f"{stem}.pkl")
    txt_path = os.path.join(output_dir, f"{stem}.txt")

    with open(pkl_path, "wb") as f:
        pickle.dump(resp, f)

    _write_raw_txt(resp, page_num, txt_path)
    return pkl_path, txt_path


def _write_raw_txt(resp, page_num: int, txt_path: str) -> None:
    """将 OCR 响应写为人类可读的文本文件"""
    sep = "=" * 60
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"{sep}\nPDF 第 {page_num + 1} 页 - GLM模型原始返回结果\n{sep}\n\n")

        if hasattr(resp, 'usage') and resp.usage:
            f.write(f"--- Usage ---\n  total_tokens: {resp.usage.total_tokens}\n\n")

        if hasattr(resp, 'data_info') and resp.data_info and resp.data_info.pages:
            f.write("--- Data Info ---\n")
            for pi, pg_info in enumerate(resp.data_info.pages):
                f.write(f"  page[{pi}]: width={pg_info.width}, height={pg_info.height}\n")
            f.write("\n")

        f.write("--- Layout Details ---\n\n")
        for page_idx, page_layouts in enumerate(resp.layout_details):
            f.write(f"  Page Index: {page_idx}\n")
            f.write(f"  Items count: {len(page_layouts)}\n")
            f.write(f"  {'-' * 50}\n")
            for item in page_layouts:
                f.write(f"\n  [Item index={item.index}]\n")
                f.write(f"    label:        {item.label}\n")
                f.write(f"    native_label: {getattr(item, 'native_label', 'N/A')}\n")
                f.write(f"    bbox_2d:      {item.bbox_2d}\n")
                f.write(f"    width:        {item.width}\n")
                f.write(f"    height:       {item.height}\n")
                f.write(f"    content:\n")
                content = item.content or "(None)"
                for line in content.split('\n'):
                    f.write(f"      {line}\n")
                f.write("\n")

        f.write(f"\n{sep}\n")
        try:
            f.write(repr(resp))
        except Exception:
            f.write("(无法序列化)")


def load_raw_result_pkl(page_num: int, raw_dir: str):
    """加载 OCR 原始结果"""
    pkl_path = os.path.join(raw_dir, f"page_{page_num + 1:04d}_raw.pkl")
    if not os.path.exists(pkl_path):
        return None
    with open(pkl_path, "rb") as f:
        return pickle.load(f)


def open_directory(path: str) -> None:
    """跨平台打开文件夹"""
    if sys.platform == 'win32':
        os.startfile(path)
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


# ============================================================
# LaTeX 预处理工具
# ============================================================

def fix_textcircled(text: str) -> str:
    """将 \\textcircled{N} 转换为 \\ding{N+171}"""

    def _replace_dollar_group(m: re.Match) -> str:
        inner = m.group(1)
        cleaned = inner.strip()
        remaining = re.sub(r'\\textcircled\{\d+}', '', cleaned).strip()

        if not remaining:
            return re.sub(
                r'\\textcircled\{(\d+)}',
                lambda m2: f"\\ding{{{int(m2.group(1)) + 171}}}",
                cleaned
            )
        else:
            replaced = re.sub(
                r'\\textcircled\{(\d+)}',
                lambda m2: f"\\text{{\\ding{{{int(m2.group(1)) + 171}}}}}",
                inner
            )
            return f"${replaced}$"

    text = re.sub(
        r'\$([^$]*\\textcircled\{[^$]*)\$',
        _replace_dollar_group, text
    )
    text = re.sub(
        r'\\textcircled\{(\d+)}',
        lambda m: f"\\ding{{{int(m.group(1)) + 171}}}",
        text
    )
    return text


_HEADING_COMMANDS = {
    1: r'\section*',
    2: r'\subsection*',
    3: r'\subsubsection*',
    4: r'\paragraph*',
    5: r'\subparagraph*',
}


def convert_markdown_headings(text: str) -> str:
    """将 Markdown 标题语法转为 LaTeX 标题命令"""
    lines = text.split('\n')
    new_lines = []
    for line in lines:
        m = re.match(r'^(#{1,6})\s+(.+)$', line.strip())
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            cmd = _HEADING_COMMANDS = {
                1: r'\section*',
                2: r'\subsection*',
                3: r'\subsubsection*',
                4: r'\paragraph*',
                5: r'\subparagraph*',
            }
def convert_markdown_headings(text: str) -> str:
    """将 Markdown 标题语法转为 LaTeX 标题命令"""
    lines = text.split('\n')
    new_lines = []
    for line in lines:
        m = re.match(r'^(#{1,6})\s+(.+)$', line.strip())
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            cmd = _HEADING_COMMANDS.get(level)
            if cmd:
                new_lines.append(f"{cmd}{{{title}}}")
            else:
                new_lines.append(f"{{\\bfseries\\large {title}}}")
        else:
            new_lines.append(line)
    return '\n'.join(new_lines)
def escape_latex(text: str) -> str:
    """转义 LaTeX 特殊字符，保护已有的 LaTeX 命令"""
    protected: List[Tuple[str, str]] = []
    counter = 0
    def _protect(m: re.Match) -> str:
        nonlocal counter
        placeholder = f"@@PROTECT{counter}@@"
        protected.append((placeholder, m.group(0)))
        counter += 1
        return placeholder
    result = text
    # 按优先级保护各种 LaTeX 结构
    protection_patterns = [
        r'\$\$.*?\$\$',                                    # display math
        r'\$.+?\$',                                        # inline math
        r'\\(?:sub)*(?:section|paragraph)\*\{[^}]*}',     # headings
        r'\\ding\{\d+}',                                   # ding symbols
        r'\{\\bfseries[^}]*}',                             # bold
        r'\\includegraphics\[[^]]*]\{[^}]*}',             # images
        r'\\(?:begin|end)\{[^}]*}',                        # environments
        r'\\[a-zA-Z]+\{[^}]*}',                           # commands with args
        r'\\[a-zA-Z]+',                                    # bare commands
    ]
    for pattern in protection_patterns:
        flags = re.DOTALL if '$$' in pattern else 0
        result = re.sub(pattern, _protect, result, flags=flags)
    # 转义特殊字符
    escape_map = [
        ('&', r'\&'), ('%', r'\%'), ('_', r'\_'),
        ('~', r'\textasciitilde{}'), ('#', r'\#'),
    ]
    for char, repl in escape_map:
        result = result.replace(char, repl)
    # 恢复被保护的内容
    for placeholder, original in protected:
        result = result.replace(placeholder, original)
    return result
def preprocess_content(text: str) -> str:
    """内容预处理流水线"""
    text = fix_textcircled(text)
    text = convert_markdown_headings(text)
    text = escape_latex(text)
    return text
def html_table_to_latex(html_str: str) -> str:
    """将 HTML 表格转换为 LaTeX tabular"""
    inner = re.sub(r'<table[^>]*>', '', html_str)
    inner = re.sub(r'</table>', '', inner)
    rows_html = re.findall(r'<tr[^>]*>(.*?)</tr>', inner, flags=re.DOTALL)
    if not rows_html:
        return html_str
    parsed_rows = []
    max_cols = 0
    for row_html in rows_html:
        cells = re.findall(
            r'<(td|th)([^>]*)>(.*?)</(?:td|th)>',
            row_html, flags=re.DOTALL
        )
        row_cells = []
        col_count = 0
        for tag, attrs, content in cells:
            content = content.strip()
            colspan = int(m.group(1)) if (m := re.search(r'colspan\s*=\s*["\']?(\d+)', attrs)) else 1
            rowspan = int(m.group(1)) if (m := re.search(r'rowspan\s*=\s*["\']?(\d+)', attrs)) else 1
            # 清理 HTML 标签
            content = re.sub(r'<br\s*/?>', ' ', content)
            content = re.sub(r'<[^>]+>', '', content).strip()
            # 保护数学环境后转义
            content = _escape_table_cell(content)
            row_cells.append({
                'content': content,
                'colspan': colspan,
                'rowspan': rowspan,
                'is_header': (tag == 'th'),
            })
            col_count += colspan
        parsed_rows.append(row_cells)
        max_cols = max(max_cols, col_count)
    if max_cols == 0:
        return html_str
    return _build_tabular(parsed_rows, max_cols)
def _escape_table_cell(content: str) -> str:
    """转义表格单元格内容，保护数学环境"""
    protected: List[Tuple[str, str]] = []
    counter = 0
    def _protect_math(m: re.Match) -> str:
        nonlocal counter
        placeholder = f"__MATH{counter}__"
        protected.append((placeholder, m.group(0)))
        counter += 1
        return placeholder
    safe = re.sub(r'\$\$.*?\$\$', _protect_math, content, flags=re.DOTALL)
    safe = re.sub(r'\$.+?\$', _protect_math, safe)
    for char, repl in [('%', r'\%'), ('_', r'\_'), ('#', r'\#'),
                       ('~', r'\textasciitilde{}')]:
        safe = safe.replace(char, repl)
    for placeholder, original in protected:
        safe = safe.replace(placeholder, original)
    return safe
def _build_tabular(parsed_rows: list, max_cols: int) -> str:
    """从解析后的行数据构建 LaTeX tabular"""
    col_spec = '|' + 'c|' * max_cols
    lines = [
        r'\begin{table}[H]',
        r'\centering',
        f'\\begin{{tabular}}{{{col_spec}}}',
        r'\hline',
    ]
    for row_cells in parsed_rows:
        cell_strs = []
        for cell in row_cells:
            text = cell['content']
            if cell['is_header']:
                text = f"\\textbf{{{text}}}"
            if cell['colspan'] > 1:
                text = f"\\multicolumn{{{cell['colspan']}}}{{|c|}}{{{text}}}"
            if cell['rowspan'] > 1:
                text = f"\\multirow{{{cell['rowspan']}}}{{*}}{{{text}}}"
            cell_strs.append(text)
        lines.append(' & '.join(cell_strs) + r' \\')
        lines.append(r'\hline')
    lines.extend([r'\end{tabular}', r'\end{table}'])
    return '\n'.join(lines)
# ============================================================
# Step1: PDF → Images
# ============================================================
def extract_pdf_page_images(
    pdf_path: str,
    output_dir: str,
    page_start: Optional[int] = None,
    page_end: Optional[int] = None,
    mode: str = "extract",
    render_dpi: int = DEFAULT_DPI,
    log_fn: Optional[LogFn] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> List[Tuple[int, str]]:
    """从 PDF 提取或渲染页面图片"""
    log = log_fn or (lambda msg: None)
    os.makedirs(output_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    try:
        total_pages = doc.page_count
        start = max(0, page_start if page_start is not None else 0)
        end = min(total_pages, page_end if page_end is not None else total_pages)
        log(f"PDF 共 {total_pages} 页，处理范围: 第 {start + 1} ~ {end} 页")
        log(f"处理模式: {'渲染页面（文字版）' if mode == 'render' else '提取嵌入图片（扫描版）'}")
        results = []
        skipped = 0
        for pg in range(start, end):
            if cancel_check and cancel_check():
                log("⚠ 用户取消操作")
                break
            img_path = os.path.join(output_dir, f"pdf_page_{pg:04d}.png")
            # 跳过已存在的文件
            if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                log(f"  第 {pg + 1} 页: 已存在，跳过")
                results.append((pg, img_path))
                skipped += 1
                continue
            page = doc[pg]
            if mode == "render":
                _render_page(page, img_path, render_dpi)
                pix_info = f"渲染完成"
            else:
                pix_info = _extract_or_render_page(doc, page, img_path, render_dpi)
            log(f"  第 {pg + 1} 页: {pix_info}")
            results.append((pg, img_path))
        log(f"共处理 {len(results)} 页 (新处理 {len(results) - skipped}, 跳过 {skipped})")
        return results
    finally:
        doc.close()
def _render_page(page, img_path: str, dpi: int) -> None:
    """渲染单页为图片"""
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    pix.save(img_path)
def _extract_or_render_page(doc, page, img_path: str, dpi: int) -> str:
    """尝试提取嵌入图片，失败则渲染"""
    image_list = page.get_images()
    if not image_list:
        _render_page(page, img_path, dpi)
        return f"无嵌入图片，渲染兜底 (DPI={dpi})"
    # 找最大的嵌入图片
    best_pix = None
    best_xref = None
    best_size = 0
    for img_info in image_list:
        xref = img_info[0]
        try:
            pix = fitz.Pixmap(doc, xref)
            size = pix.width * pix.height
            if size > best_size:
                best_size = size
                best_xref = xref
                best_pix = pix
        except Exception:
            continue
    if best_pix is not None:
        if best_pix.n - best_pix.alpha > 3:
            best_pix = fitz.Pixmap(fitz.csRGB, best_pix)
        best_pix.save(img_path)
        return f"xref={best_xref}, 尺寸={best_pix.width}x{best_pix.height}"
    else:
        _render_page(page, img_path, dpi)
        return "提取失败，渲染兜底"
# ============================================================
# Step2: OCR
# ============================================================
def call_layout_parsing(image_path: str, api_key: str):
    """调用智谱 AI 版面解析 API"""
    from zai import ZhipuAiClient
    client = ZhipuAiClient(api_key=api_key)
    base64_image = img_to_data_url(image_path)
    return client.layout_parsing.create(model="glm-ocr", file=base64_image)
def extract_layout_images(
    image_path: str,
    resp,
    output_dir: str,
    filename_prefix: str = "",
    log_fn: Optional[LogFn] = None,
) -> Dict:
    """从版面解析结果中截取图片区域"""
    log = log_fn or (lambda msg: None)
    os.makedirs(output_dir, exist_ok=True)
    page_image = Image.open(image_path)
    actual_w, actual_h = page_image.size
    saved_images = {}
    img_count = 0
    for page_idx, page_layouts in enumerate(resp.layout_details):
        for item in page_layouts:
            if item.label != "image":
                continue
            x1, y1, x2, y2 = item.bbox_2d
            scale_x = actual_w / item.width
            scale_y = actual_h / item.height
            crop_box = (
                max(0, int(x1 * scale_x)),
                max(0, int(y1 * scale_y)),
                min(actual_w, int(x2 * scale_x)),
                min(actual_h, int(y2 * scale_y)),
            )
            cropped = page_image.crop(crop_box)
            filename = f"{filename_prefix}page{page_idx}_idx{item.index}_{item.native_label}.png"
            save_path = os.path.join(output_dir, filename)
            cropped.save(save_path)
            saved_images[item.index] = {
                "path": f"images/{filename}",
                "item": item,
                "page_idx": page_idx,
            }
            log(f"    截取: {filename}")
            img_count += 1
    page_image.close()
    log(f"  本页截取 {img_count} 张图片")
    return saved_images
def run_step2_ocr(
    config: PipelineConfig,
    log_fn: Optional[LogFn] = None,
    progress_fn: Optional[ProgressFn] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> None:
    """Step2: 对页面图片执行 OCR 识别"""
    log = log_fn or (lambda msg: None)
    page_images = scan_page_images(config.pdf_pages_dir)
    if not page_images:
        log(f"错误: {config.pdf_pages_dir} 中没有找到页面图片")
        return
    # 过滤页码范围
    if config.page_start is not None and config.page_end is not None:
        page_images = [
            (pn, pp) for pn, pp in page_images
            if config.page_start <= pn < config.page_end
        ]
    completed = scan_completed_raw_results(config.raw_results_dir)
    todo = [(pn, pp) for pn, pp in page_images if pn not in completed]
    log(f"页面图片总数: {len(page_images)}, 已完成: {len(completed)}, 待处理: {len(todo)}")
    if not todo:
        log("所有页面已处理完毕！")
        return

    os.makedirs(config.raw_results_dir, exist_ok=True)
    os.makedirs(config.extracted_images_dir, exist_ok=True)

    total_tokens = 0
    success_count = 0
    failed_pages: List[Tuple[int, str]] = []

    for i, (page_num, page_img_path) in enumerate(todo):
        if cancel_check and cancel_check():
            log("⚠ 用户取消操作")
            break

        if progress_fn:
            progress_fn(i, len(todo))

        log(f"{'─' * 40}")
        log(f"处理第 {page_num + 1} 页 ({i + 1}/{len(todo)})")

        # 验证图片有效性
        error = _validate_image(page_img_path)
        if error:
            log(f"  {error}")
            failed_pages.append((page_num, error))
            continue

        # 调用 API
        try:
            log(f"  调用版面解析 API...")
            resp = call_layout_parsing(page_img_path, config.api_key)
            tokens = resp.usage.total_tokens
            log(f"  成功! tokens={tokens}")
            total_tokens += tokens
        except Exception as e:
            log(f"  API 调用失败: {e}")
            log(traceback.format_exc())
            failed_pages.append((page_num, str(e)))
            continue

        # 保存结果
        pkl_path, _ = save_raw_result(resp, page_num, config.raw_results_dir)
        log(f"  保存: {os.path.basename(pkl_path)}")

        # 截取图片
        filename_prefix = f"pdfp{page_num:04d}_"
        extract_layout_images(
            page_img_path, resp, config.extracted_images_dir,
            filename_prefix=filename_prefix, log_fn=log_fn,
        )
        success_count += 1

    if progress_fn:
        progress_fn(len(todo), len(todo))

    log(f"\n步骤2完成! 成功: {success_count}, 失败: {len(failed_pages)}, 消耗tokens: {total_tokens}")
    _report_failures(failed_pages, log)


def _validate_image(image_path: str) -> Optional[str]:
    """验证图片文件有效性，返回错误信息或 None"""
    try:
        with Image.open(image_path) as img:
            w, h = img.size
            if w < MIN_IMAGE_DIMENSION or h < MIN_IMAGE_DIMENSION:
                return f"警告: 图片太小 ({w}x{h})，跳过"
    except Exception as e:
        return f"无法打开图片，跳过: {e}"
    return None


def _report_failures(failed_pages: List[Tuple[int, str]], log: LogFn) -> None:
    """统一报告失败页面"""
    if failed_pages:
        for pg, reason in failed_pages:
            log(f"  失败: 第 {pg + 1} 页 - {reason}")


# ============================================================
# Step3: Raw results → TeX
# ============================================================

def build_saved_images_for_page(
    page_num: int, resp, extracted_images_dir: str
) -> Dict:
    """从文件系统重建某页的图片索引"""
    saved_images = {}
    filename_prefix = f"pdfp{page_num:04d}_"

    for page_idx, page_layouts in enumerate(resp.layout_details):
        for item in page_layouts:
            if item.label != "image":
                continue
            filename = f"{filename_prefix}page{page_idx}_idx{item.index}_{item.native_label}.png"
            img_full_path = os.path.join(extracted_images_dir, filename)
            if os.path.exists(img_full_path):
                saved_images[item.index] = {
                    "path": f"images/{filename}",
                    "item": item,
                    "page_idx": page_idx,
                }
    return saved_images


def generate_page_tex_body(resp, saved_images: Dict) -> List[str]:
    """根据版面解析结果生成单页 TeX 内容"""
    layout_details = resp.layout_details

    # 获取版面尺寸
    if resp.data_info and resp.data_info.pages:
        layout_w = resp.data_info.pages[0].width
        layout_h = resp.data_info.pages[0].height
    else:
        first_item = layout_details[0][0]
        layout_w = first_item.width
        layout_h = first_item.height

    tex_lines: List[str] = []

    for page_idx, page_layouts in enumerate(layout_details):
        sorted_items = sorted(page_layouts, key=lambda x: x.index)
        i = 0
        while i < len(sorted_items):
            item = sorted_items[i]

            if item.label == "image" and item.index in saved_images:
                i = _emit_image_items(
                    tex_lines, sorted_items, i, saved_images,
                    layout_w, layout_h,
                )
            elif item.label == "formula" and item.content:
                _emit_formula(tex_lines, item)
                i += 1
            elif item.label == "table" and item.content:
                _emit_table(tex_lines, item)
                i += 1
            elif item.label == "text" and item.content:
                _emit_text(tex_lines, item)
                i += 1
            else:
                i += 1

    return tex_lines


def _emit_image_items(
    tex_lines: List[str],
    sorted_items: list,
    start_idx: int,
    saved_images: Dict,
    layout_w: float,
    layout_h: float,
) -> int:
    """处理图片元素（含同行多图合并），返回下一个待处理索引"""
    item = sorted_items[start_idx]

    # 查找同行图片
    row_images = [item]
    j = start_idx + 1
    while j < len(sorted_items):
        next_item = sorted_items[j]
        if (next_item.label == "image"
                and next_item.index in saved_images
                and abs(next_item.bbox_2d[1] - item.bbox_2d[1]) < layout_h * SAME_ROW_Y_TOLERANCE):
            row_images.append(next_item)
            j += 1
        else:
            break

    if len(row_images) > 1:
        # 同行多图
        tex_lines.append("")
        tex_lines.append(r"\begin{figure}[H]")
        tex_lines.append(r"\centering")
        total_width_ratio = sum(
            (img.bbox_2d[2] - img.bbox_2d[0]) / layout_w
            for img in row_images
        )
        for k, img in enumerate(row_images):
            img_path = saved_images[img.index]["path"]
            img_ratio = (img.bbox_2d[2] - img.bbox_2d[0]) / layout_w
            tex_width = _clamp(
                (img_ratio / total_width_ratio) * IMG_ROW_AVAILABLE_WIDTH,
                IMG_WIDTH_MIN, 0.9,
            )
            if k > 0:
                tex_lines.append(r"\hfill")
            tex_lines.append(
                f"\\includegraphics[width={tex_width:.3f}\\textwidth]{{{img_path}}}"
            )
        tex_lines.append(r"\end{figure}")
        tex_lines.append("")
    else:
        # 单图
        img_path = saved_images[item.index]["path"]
        img_width_ratio = (item.bbox_2d[2] - item.bbox_2d[0]) / layout_w
        tex_width = _clamp(img_width_ratio, IMG_WIDTH_MIN, IMG_WIDTH_MAX)

        tex_lines.append("")
        tex_lines.append(r"\begin{figure}[H]")

        # 根据图片水平位置决定对齐方式
        center_x = (item.bbox_2d[0] + item.bbox_2d[2]) / 2
        if center_x < layout_w * IMAGE_CENTER_LEFT_THRESHOLD:
            tex_lines.append(r"\raggedright")
        elif center_x > layout_w * IMAGE_CENTER_RIGHT_THRESHOLD:
            tex_lines.append(r"\raggedleft")
        else:
            tex_lines.append(r"\centering")

        tex_lines.append(
            f"\\includegraphics[width={tex_width:.3f}\\textwidth]{{{img_path}}}"
        )
        tex_lines.append(r"\end{figure}")
        tex_lines.append("")

    return j


def _emit_formula(tex_lines: List[str], item) -> None:
    """输出公式元素"""
    content = fix_textcircled(item.content.strip())
    if content.startswith("$$") and content.endswith("$$"):
        inner = content[2:-2].strip()
        tex_lines.append(r"\begin{equation*}")
        tex_lines.append(f"  {inner}")
        tex_lines.append(r"\end{equation*}")
    else:
        tex_lines.append(content)
    tex_lines.append("")


def _emit_table(tex_lines: List[str], item) -> None:
    """输出表格元素"""
    content = item.content.strip()
    if '<table' in content.lower():
        tex_lines.append("")
        tex_lines.append(html_table_to_latex(content))
        tex_lines.append("")
    else:
        tex_lines.append(preprocess_content(content))
        tex_lines.append("")


def _emit_text(tex_lines: List[str], item) -> None:
    """输出文本元素"""
    content = item.content.strip()
    content = re.sub(r'<div[^>]*>', '', content)
    content = re.sub(r'</div>', '', content).strip()
    if content:
        tex_lines.append(preprocess_content(content))
        tex_lines.append("")


def _clamp(value: float, min_val: float, max_val: float) -> float:
    """将值限制在 [min_val, max_val] 范围内"""
    return max(min_val, min(max_val, value))


def run_step3_to_tex(
    config: PipelineConfig,
    force: bool = False,
    log_fn: Optional[LogFn] = None,
    progress_fn: Optional[ProgressFn] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> None:
    """Step3: 将 OCR 原始结果转换为单页 TeX 片段"""
    log = log_fn or (lambda msg: None)

    completed_raw = scan_completed_raw_results(config.raw_results_dir)
    if not completed_raw:
        log(f"错误: {config.raw_results_dir} 中没有找到原始结果文件")
        return

    page_nums = sorted(completed_raw)
    if config.page_start is not None and config.page_end is not None:
        page_nums = [pn for pn in page_nums if config.page_start <= pn < config.page_end]

    if not force:
        completed_tex = scan_completed_tex_pages(config.tex_pages_dir)
        todo = [pn for pn in page_nums if pn not in completed_tex]
    else:
        todo = page_nums

    log(f"原始结果总数: {len(page_nums)}, 待转换: {len(todo)}")

    if not todo:
        log("所有页面已转换完毕！")
        return

    os.makedirs(config.tex_pages_dir, exist_ok=True)
    success_count = 0
    failed_pages: List[Tuple[int, str]] = []

    for i, page_num in enumerate(todo):
        if cancel_check and cancel_check():
            log("⚠ 用户取消操作")
            break

        if progress_fn:
            progress_fn(i, len(todo))

        log(f"转换第 {page_num + 1} 页 ({i + 1}/{len(todo)})...")
        resp = load_raw_result_pkl(page_num, config.raw_results_dir)
        if resp is None:
            log(f"  失败: pkl文件不存在")
            failed_pages.append((page_num, "pkl文件不存在"))
            continue

        try:
            saved_images = build_saved_images_for_page(
                page_num, resp, config.extracted_images_dir
            )
            tex_lines = generate_page_tex_body(resp, saved_images)
            tex_path = os.path.join(config.tex_pages_dir, f"page_{page_num + 1:04d}.tex")

            with open(tex_path, "w", encoding="utf-8") as f:
                f.write(f"% ========== PDF 第 {page_num + 1} 页 ==========\n")
                f.write("\n".join(tex_lines))
                f.write("\n")

            log(f"  OK ({len(tex_lines)} 行)")
            success_count += 1
        except Exception as e:
            log(f"  失败: {e}")
            log(traceback.format_exc())
            failed_pages.append((page_num, str(e)))

    if progress_fn:
        progress_fn(len(todo), len(todo))

    log(f"\n步骤3完成! 成功: {success_count}, 失败: {len(failed_pages)}")
    _report_failures(failed_pages, log)


# ============================================================
# Step4: Merge
# ============================================================

def run_step4_merge(
    config: PipelineConfig,
    log_fn: Optional[LogFn] = None,
) -> None:
    """Step4: 合并所有 TeX 片段为完整文档"""
    log = log_fn or (lambda msg: None)

    tex_pages = scan_tex_pages(config.tex_pages_dir)
    if not tex_pages:
        log(f"错误: {config.tex_pages_dir} 中没有找到 tex 片段文件")
        return

    log(f"找到 {len(tex_pages)} 个页面片段")
    log(f"页码范围: 第 {tex_pages[0][0]} ~ {tex_pages[-1][0]} 页")

    # 检测缺失页
    page_numbers = [pn for pn, _ in tex_pages]
    expected = set(range(page_numbers[0], page_numbers[-1] + 1))
    missing = expected - set(page_numbers)
    if missing:
        log(f"⚠ 警告: 以下页码缺失: {sorted(missing)}")

    # 合并
    merged_parts = [TEX_PREAMBLE]
    for page_num, filepath in tex_pages:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                merged_parts.append(f.read())
            merged_parts.append("")
        except Exception as e:
            log(f"⚠ 读取第 {page_num} 页失败: {e}")
            merged_parts.append(f"% ========== 第 {page_num} 页: 读取失败 ==========\n")
    merged_parts.append(TEX_ENDING)
    output_path = config.output_tex_file
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(merged_parts))
    total_size = os.path.getsize(output_path)
    total_lines = sum(1 for _ in open(output_path, encoding="utf-8"))
    log(f"\n步骤4完成!")
    log(f"合并页数: {len(tex_pages)}")
    log(f"输出文件: {output_path}")
    log(f"文件大小: {total_size:,} 字节")
    log(f"总行数: {total_lines:,} 行")
# ============================================================
# 信号桥：线程安全地向 GUI 发送日志和进度
# ============================================================
class WorkerSignals(QObject):
    """后台线程与 GUI 之间的通信信号"""
    log_signal = pyqtSignal(str, int)       # message, LogLevel.value
    progress_signal = pyqtSignal(int, int)  # current, total
    finished_signal = pyqtSignal(bool, str) # success, message
    step_finished_signal = pyqtSignal(int)  # step number
# ============================================================
# GUI 主窗口
# ============================================================
STEP_NAMES = {
    1: "PDF→图片",
    2: "OCR识别",
    3: "生成TeX",
    4: "合并输出",
}
class PDFtoTeXApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF → LaTeX 转换工具")
        self.setMinimumSize(900, 700)
        self.resize(1050, 780)
        # 状态
        self.pdf_path = ""
        self.output_dir = ""
        self.is_running = False
        self._cancel_event = threading.Event()
        # 信号
        self.signals = WorkerSignals()
        self.signals.log_signal.connect(self._append_log)
        self.signals.progress_signal.connect(self._update_progress)
        self.signals.finished_signal.connect(self._on_finished)
        self.signals.step_finished_signal.connect(self._on_step_finished)
        self._build_ui()
        self._apply_style()
    # ─── UI 构建 ──────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.addWidget(self._build_file_group())
        main_layout.addWidget(self._build_param_group())
        main_layout.addWidget(self._build_action_group())
        main_layout.addLayout(self._build_progress_row())
        main_layout.addWidget(self._build_log_group(), 1)
    def _build_file_group(self) -> QGroupBox:
        """文件选择区"""
        group = QGroupBox("📂 文件设置")
        layout = QVBoxLayout(group)
        # PDF 选择
        self.pdf_label = QLabel("未选择")
        self.pdf_label.setStyleSheet("color: #666; font-style: italic;")
        self.pdf_label.setMinimumWidth(400)
        self.btn_select_pdf = QPushButton("选择 PDF")
        self.btn_select_pdf.setFixedWidth(120)
        self.btn_select_pdf.clicked.connect(self._select_pdf)
        layout.addLayout(self._make_labeled_row("PDF 文件:", self.pdf_label, self.btn_select_pdf))
        # 输出目录
        self.out_label = QLabel("未选择")
        self.out_label.setStyleSheet("color: #666; font-style: italic;")
        self.out_label.setMinimumWidth(400)
        self.btn_select_output = QPushButton("选择目录")
        self.btn_select_output.setFixedWidth(120)
        self.btn_select_output.clicked.connect(self._select_output_dir)
        layout.addLayout(self._make_labeled_row("输出目录:", self.out_label, self.btn_select_output))
        return group
    def _build_param_group(self) -> QGroupBox:
        """参数设置区"""
        group = QGroupBox("⚙️ 参数设置")
        layout = QHBoxLayout(group)
        # 模式
        layout.addWidget(QLabel("处理模式:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["render (文字版PDF)", "extract (扫描版PDF)"])
        self.mode_combo.setFixedWidth(200)
        layout.addWidget(self.mode_combo)
        layout.addSpacing(20)
        # DPI
        layout.addWidget(QLabel("渲染 DPI:"))
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(72, 1200)
        self.dpi_spin.setValue(DEFAULT_DPI)
        self.dpi_spin.setSingleStep(50)
        self.dpi_spin.setFixedWidth(80)
        layout.addWidget(self.dpi_spin)
        layout.addSpacing(20)
        # 页码范围
        layout.addWidget(QLabel("起始页(0起):"))
        self.page_start_spin = QSpinBox()
        self.page_start_spin.setRange(0, 99999)
        self.page_start_spin.setValue(0)
        self.page_start_spin.setFixedWidth(70)
        layout.addWidget(self.page_start_spin)
        layout.addWidget(QLabel("结束页:"))
        self.page_end_spin = QSpinBox()
        self.page_end_spin.setRange(0, 99999)
        self.page_end_spin.setValue(0)
        self.page_end_spin.setSpecialValueText("全部")
        self.page_end_spin.setFixedWidth(70)
        layout.addWidget(self.page_end_spin)
        layout.addSpacing(20)
        # API Key
        layout.addWidget(QLabel("API Key:"))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setPlaceholderText("请输入智谱AI API Key")
        # 优先从环境变量读取
        env_key = os.environ.get("ZHIPU_API_KEY", "")
        if env_key:
            self.api_key_edit.setText(env_key)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setMinimumWidth(150)
        layout.addWidget(self.api_key_edit, 1)
        self.btn_show_key = QPushButton("👁")
        self.btn_show_key.setFixedWidth(30)
        self.btn_show_key.setCheckable(True)
        self.btn_show_key.clicked.connect(self._toggle_key_visibility)
        layout.addWidget(self.btn_show_key)
        return group
    def _build_action_group(self) -> QGroupBox:
        """操作按钮区"""
        group = QGroupBox("🚀 操作")
        layout = QHBoxLayout(group)
        # 一键执行
        self.btn_run_all = QPushButton("▶ 一键全部执行 (Step 1→4)")
        self.btn_run_all.setFixedHeight(40)
        self.btn_run_all.setObjectName("primaryButton")
        self.btn_run_all.clicked.connect(self._run_all)
        layout.addWidget(self.btn_run_all)
        layout.addSpacing(10)
        # 单步按钮
        self.step_buttons: Dict[int, QPushButton] = {}
        for step_num, step_name in STEP_NAMES.items():
            btn = QPushButton(f"Step{step_num}\n{step_name}")
            btn.setFixedHeight(40)
            btn.clicked.connect(lambda checked, s=step_num: self._run_single_step(s))
            layout.addWidget(btn)
            self.step_buttons[step_num] = btn
        layout.addSpacing(10)
        # 取消按钮
        self.btn_cancel = QPushButton("⏹ 取消")
        self.btn_cancel.setFixedHeight(40)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setObjectName("cancelButton")
        self.btn_cancel.clicked.connect(self._cancel_task)
        layout.addWidget(self.btn_cancel)
        # 打开目录
        self.btn_open_output = QPushButton("📁 打开输出目录")
        self.btn_open_output.setFixedHeight(40)
        self.btn_open_output.clicked.connect(self._open_output_dir)
        layout.addWidget(self.btn_open_output)
        return group
    def _build_progress_row(self) -> QHBoxLayout:
        """进度条行"""
        layout = QHBoxLayout()
        self.progress_label = QLabel("就绪")
        layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(22)
        layout.addWidget(self.progress_bar, 1)
        return layout
    def _build_log_group(self) -> QGroupBox:
        """日志区"""
        group = QGroupBox("📋 运行日志")
        layout = QVBoxLayout(group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))
        self.log_text.setStyleSheet(
            "QTextEdit { background-color: #1e1e1e; color: #d4d4d4; "
            "border: 1px solid #555; border-radius: 4px; }"
        )
        layout.addWidget(self.log_text)
        # 日志工具栏
        toolbar = QHBoxLayout()
        btn_clear = QPushButton("清空日志")
        btn_clear.clicked.connect(self.log_text.clear)
        toolbar.addWidget(btn_clear)
        btn_copy = QPushButton("复制日志")
        btn_copy.clicked.connect(self._copy_log)
        toolbar.addWidget(btn_copy)
        toolbar.addStretch()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #888;")
        toolbar.addWidget(self.status_label)
        layout.addLayout(toolbar)
        return group
    @staticmethod
    def _make_labeled_row(label_text: str, content_widget: QWidget, button: QPushButton) -> QHBoxLayout:
        """创建 "标签 + 内容 + 按钮" 的水平行"""
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        row.addWidget(content_widget, 1)
        row.addWidget(button)
        return row
    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #f5f5f5; }
            QGroupBox {
                font-weight: bold; font-size: 13px;
                border: 1px solid #ccc; border-radius: 6px;
                margin-top: 8px; padding-top: 16px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px; padding: 0 6px;
            }
            QPushButton {
                background-color: #e0e0e0; border: 1px solid #bbb;
                border-radius: 4px; padding: 4px 12px; font-size: 12px;
            }
            QPushButton:hover { background-color: #d0d0d0; }
            QPushButton:pressed { background-color: #c0c0c0; }
            QPushButton:disabled { background-color: #f0f0f0; color: #aaa; }
            QPushButton#primaryButton {
                background-color: #4CAF50; color: white;
                font-size: 14px; font-weight: bold; border-radius: 6px;
            }
            QPushButton#primaryButton:hover { background-color: #45a049; }
            QPushButton#primaryButton:disabled { background-color: #ccc; color: #888; }
            QPushButton#cancelButton {
                background-color: #f44336; color: white;
                font-weight: bold; border-radius: 6px;
            }
            QPushButton#cancelButton:hover { background-color: #d32f2f; }
            QPushButton#cancelButton:disabled { background-color: #f0f0f0; color: #aaa; }
            QLabel { font-size: 12px; }
            QSpinBox, QComboBox, QLineEdit {
                border: 1px solid #bbb; border-radius: 3px;
                padding: 3px 6px; font-size: 12px;
            }
            QProgressBar {
                border: 1px solid #bbb; border-radius: 4px;
                text-align: center; font-size: 11px;
            }
            QProgressBar::chunk {
                background-color: #4CAF50; border-radius: 3px;
            }
        """)
    # ─── 事件处理 ─────────────────────────────────────────
    def _select_pdf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 PDF 文件", "", "PDF 文件 (*.pdf);;所有文件 (*)"
        )
        if not path:
            return
        self.pdf_path = path
        self.pdf_label.setText(path)
        self.pdf_label.setStyleSheet("color: #333; font-style: normal; font-weight: bold;")
        self._log(f"已选择 PDF: {path}")
        try:
            doc = fitz.open(path)
            total = doc.page_count
            doc.close()
            self.page_end_spin.setMaximum(total)
            self.page_start_spin.setMaximum(total - 1)
            self._log(f"PDF 共 {total} 页")
            # 自动建议输出目录
            if not self.output_dir:
                suggested = os.path.join(
                    os.path.dirname(path),
                    Path(path).stem + "_output",
                )
                self.output_dir = suggested
                self.out_label.setText(suggested)
                self.out_label.setStyleSheet("color: #333; font-style: normal;")
                self._log(f"自动建议输出目录: {suggested}")
        except Exception as e:
            self._log(f"读取 PDF 信息失败: {e}", LogLevel.ERROR)

    def _select_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", "")
        if path:
            self.output_dir = path
            self.out_label.setText(path)
            self.out_label.setStyleSheet("color: #333; font-style: normal; font-weight: bold;")
            self._log(f"输出目录: {path}")

    def _toggle_key_visibility(self):
        if self.btn_show_key.isChecked():
            self.api_key_edit.setEchoMode(QLineEdit.Normal)
        else:
            self.api_key_edit.setEchoMode(QLineEdit.Password)

    def _open_output_dir(self):
        if self.output_dir and os.path.isdir(self.output_dir):
            open_directory(self.output_dir)
        else:
            QMessageBox.warning(self, "提示", "输出目录不存在或未选择。")

    def _copy_log(self):
        QApplication.clipboard().setText(self.log_text.toPlainText())
        self.status_label.setText("日志已复制到剪贴板")

    def _cancel_task(self):
        """请求取消当前任务"""
        if self.is_running:
            self._cancel_event.set()
            self._log("正在取消任务...", LogLevel.WARNING)
            self.btn_cancel.setEnabled(False)

    # ─── 日志系统 ─────────────────────────────────────────
    def _log(self, msg: str, level: LogLevel = LogLevel.INFO):
        """主线程直接写日志"""
        color = LOG_COLORS.get(level, LOG_COLORS[LogLevel.INFO])
        if level == LogLevel.HEADER:
            self.log_text.append(
                f'<br><span style="color:{color}; font-size:14px; font-weight:bold;">'
                f'{msg}</span>'
            )
        else:
            self.log_text.append(f'<span style="color:{color};">{msg}</span>')
        self._scroll_log_to_bottom()

    def _scroll_log_to_bottom(self):
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _append_log(self, msg: str, level_value: int):
        """线程安全的日志追加（通过信号调用）"""
        try:
            level = LogLevel(level_value)
        except ValueError:
            level = LogLevel.INFO
        self._log(msg, level)

    def _update_progress(self, current: int, total: int):
        if total > 0:
            pct = int(current / total * 100)
            self.progress_bar.setValue(pct)
            self.progress_label.setText(f"进度: {current}/{total}")

    def _on_finished(self, success: bool, message: str):
        self.is_running = False
        self._cancel_event.clear()
        self._set_buttons_enabled(True)
        self.btn_cancel.setEnabled(False)
        if success:
            self._log(f"\n✅ {message}", LogLevel.SUCCESS)
            self.progress_label.setText("完成!")
            self.progress_bar.setValue(100)
            QMessageBox.information(self, "完成", message)
        else:
            self._log(f"\n❌ {message}", LogLevel.ERROR)
            self.progress_label.setText("出错")
            QMessageBox.critical(self, "错误", message)

    def _on_step_finished(self, step_num: int):
        self._log(f"✅ 步骤 {step_num} 完成!", LogLevel.SUCCESS)

    def _set_buttons_enabled(self, enabled: bool):
        """统一切换所有操作按钮的可用状态"""
        widgets = [
            self.btn_run_all, self.btn_select_pdf, self.btn_select_output,
            *self.step_buttons.values(),
        ]
        for w in widgets:
            w.setEnabled(enabled)

    # ─── 配置收集 ─────────────────────────────────────────
    def _build_config(self) -> PipelineConfig:
        """从 UI 控件收集参数，构建配置对象"""
        mode_text = self.mode_combo.currentText()
        page_start_val = self.page_start_spin.value()
        page_end_val = self.page_end_spin.value()
        return PipelineConfig(
            pdf_path=self.pdf_path,
            output_dir=self.output_dir,
            mode="render" if "render" in mode_text else "extract",
            dpi=self.dpi_spin.value(),
            page_start=page_start_val if page_start_val > 0 else None,
            page_end=page_end_val if page_end_val > 0 else None,
            api_key=self.api_key_edit.text().strip(),
        )

    # ─── 任务启动 ─────────────────────────────────────────
    def _run_all(self):
        self._start_worker(steps=[1, 2, 3, 4])

    def _run_single_step(self, step: int):
        self._start_worker(steps=[step])

    def _start_worker(self, steps: List[int]):
        config = self._build_config()
        error = config.validate(steps)
        if error:
            QMessageBox.warning(self, "提示", error)
            return
        if self.is_running:
            QMessageBox.warning(self, "提示", "有任务正在运行，请等待完成。")
            return
        self.is_running = True
        self._cancel_event.clear()
        self._set_buttons_enabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText("准备中...")
        desc = " → ".join([f"Step{s}({STEP_NAMES[s]})" for s in steps])
        self._log(f"\n🚀 开始执行: {desc}")
        self._log(f"   PDF: {config.pdf_path}")
        self._log(f"   输出: {config.output_dir}")
        self._log(f"   模式: {config.mode}, DPI: {config.dpi}")
        thread = threading.Thread(
            target=self._worker_thread,
            args=(steps, config),
            daemon=True,
        )
        thread.start()

    def _worker_thread(self, steps: List[int], config: PipelineConfig):
        """后台线程：依次执行各步骤"""

        def log_fn(msg: str, level: LogLevel = LogLevel.INFO):
            self.signals.log_signal.emit(msg, level.value)

        def progress_fn(current: int, total: int):
            self.signals.progress_signal.emit(current, total)

        def cancel_check() -> bool:
            return self._cancel_event.is_set()

        # 自动检测日志级别的包装器
        def auto_log(msg: str):
            if any(kw in msg for kw in ("错误", "失败", "严重")):
                log_fn(msg, LogLevel.ERROR)
            elif any(kw in msg for kw in ("警告", "⚠")):
                log_fn(msg, LogLevel.WARNING)
            elif any(kw in msg for kw in ("完成", "成功", "OK")):
                log_fn(msg, LogLevel.SUCCESS)
            else:
                log_fn(msg, LogLevel.INFO)

        try:
            for step in steps:
                if cancel_check():
                    self.signals.finished_signal.emit(False, "任务已取消")
                    return
                step_name = STEP_NAMES[step]
                sep = "=" * 50
                log_fn(f"\n{sep}", LogLevel.HEADER)
                log_fn(f"【步骤{step}】{step_name}", LogLevel.HEADER)
                log_fn(sep, LogLevel.HEADER)
                if step == 1:
                    results = extract_pdf_page_images(
                        pdf_path=config.pdf_path,
                        output_dir=config.pdf_pages_dir,
                        page_start=config.page_start,
                        page_end=config.page_end,
                        mode=config.mode,
                        render_dpi=config.dpi,
                        log_fn=auto_log,
                        cancel_check=cancel_check,
                    )
                    auto_log(f"步骤1完成! 共 {len(results)} 页图片")
                elif step == 2:
                    run_step2_ocr(
                        config=config,
                        log_fn=auto_log,
                        progress_fn=progress_fn,
                        cancel_check=cancel_check,
                    )
                elif step == 3:
                    run_step3_to_tex(
                        config=config,
                        force=False,
                        log_fn=auto_log,
                        progress_fn=progress_fn,
                        cancel_check=cancel_check,
                    )
                elif step == 4:
                    run_step4_merge(
                        config=config,
                        log_fn=auto_log,
                    )
                if not cancel_check():
                    self.signals.step_finished_signal.emit(step)
            # 检查是否被取消
            if cancel_check():
                self.signals.finished_signal.emit(False, "任务已取消")
            else:
                desc = ", ".join([f"Step{s}" for s in steps])
                self.signals.finished_signal.emit(
                    True,
                    f"所有步骤执行完成 ({desc})!\n输出文件: {config.output_tex_file}",
                )
        except Exception as e:
            tb = traceback.format_exc()
            log_fn(f"严重错误:\n{tb}", LogLevel.ERROR)
            self.signals.finished_signal.emit(False, f"执行出错: {str(e)}")
# ============================================================
# 启动入口
# ============================================================
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = PDFtoTeXApp()
    window.show()
    sys.exit(app.exec_())
if __name__ == "__main__":
    main()
