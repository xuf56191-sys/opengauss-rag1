"""Markdown heading-first chunker. Hand-written, no LangChain."""
# 模块说明：这是一个基于 Markdown 标题优先的文本分块器
# 手写实现，不依赖 LangChain，保持轻量和可控

import re
# 导入正则表达式模块，用于解析 Markdown 标题和分割段落
from dataclasses import dataclass
# 导入 dataclass 装饰器，用于简化定义只包含数据的类
from pathlib import Path
# 导入 Path 类，用于处理文件路径，比传统的 os.path 更面向对象
from typing import List
# 导入 List 类型提示，用于标注列表类型


@dataclass
class Chunk:
    """定义一个数据类，表示一个文本块（chunk）"""
    text: str           # 文本块的实际内容
    source: str         # 来源文件路径
    heading_path: str   # 该文本块所属的标题路径（比如 "安装指南"）
    chunk_index: int    # 该文本块在整个文档中的顺序索引


# 正则表达式：匹配 Markdown 中的 1~3 级标题（如 # 一级标题、## 二级标题、### 三级标题）
# ^(#{1,3}) 表示行首的 1 到 3 个 # 号
# \s+ 表示 # 号后的空白字符
# (.+?) 表示非贪婪匹配标题文字
# \s*$ 表示行尾
# re.M 表示多行模式，让 ^ 和 $ 能匹配每一行的开头和结尾
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.M)


def _split_paragraphs(text: str) -> List[str]:
    """将文本按空行拆分成段落列表"""
    # 使用 \n\s*\n 匹配空行（换行 + 空白字符 + 换行）
    parts = re.split(r"\n\s*\n", text.strip())
    # 去除每个段落的首尾空白，并过滤掉空字符串
    return [p.strip() for p in parts if p.strip()]


def _join_with_overlap(paras: List[str], max_chars: int, overlap_chars: int) -> List[str]:
    """贪心算法：将段落打包成不超过 max_chars 的块，块之间保留少量重叠 (light overlap)"""
    if not paras:
        return []

    blocks: List[str] = []
    current = paras[0]  # 用第一个段落初始化当前块

    for p in paras[1:]:
        candidate = current + "\n\n" + p  # 尝试把当前段落拼接到当前块后面

        if len(candidate) <= max_chars:
            current = candidate  # 如果拼接后不超过限制，就接受拼接
        else:
            blocks.append(current)  # 如果超限，把当前的块存起来

            # 处理重叠：取上一个块末尾的 overlap_chars 个字符
            tail = current[-overlap_chars:] if overlap_chars > 0 else ""

            # 把尾部重叠内容拼接到新段落前面，形成下一个块
            current = (tail + "\n\n" + p).strip() if tail else p

    if current:
        blocks.append(current)  # 把最后一个块加进去

    return blocks


def chunk_markdown(
    text: str,
    source: str,
    max_chars: int = 1200,
    overlap_chars: int = 120,
) -> List[Chunk]:
    """
    max_chars≈1200 roughly maps to ~500-600 Chinese tokens for bge-m3.
    Tune later in eval phase; start here.
    """
    # 找到文本中所有的标题匹配
    matches = list(HEADING_RE.finditer(text))
    sections: List[tuple[str, str]] = []  # 存放 (标题路径, 正文内容) 的列表

    if not matches:
        # 如果没有任何标题，整个文本作为一个 section，路径为空
        sections.append(("", text.strip()))
    else:
        # 处理第一个标题之前的前言部分
        pre = text[: matches[0].start()].strip()
        if pre:
            sections.append(("", pre))

        # 遍历每一个标题
        for i, m in enumerate(matches):
            level = len(m.group(1))       # 标题级别（# 的个数）
            title = m.group(2).strip()    # 标题文字
            start = m.start()             # 标题开始的索引
            # 结束索引：如果是最后一个标题，就取文本末尾；否则取下一个标题的开始
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip() # 截取该标题下的正文内容

            # 这里只简单地把当前标题作为路径
            # 注：代码注释说可以继承最近的 H1/H2，但这里简化处理，只使用当前标题
            if level <= 2:
                path = title
            else:
                path = title
            sections.append((path, body))

    chunks: List[Chunk] = []
    idx = 0  # 全局块索引

    # 遍历所有 section，对每个 section 进行分块
    for path, body in sections:
        if not body:
            continue  # 跳过空内容

        # 如果 section 内容不超过最大字符数，直接作为一个块
        if len(body) <= max_chars:
            blocks = [body]
        else:
            # 否则，先拆分成段落，再用贪心算法合并成多个块
            blocks = _join_with_overlap(
                _split_paragraphs(body), max_chars=max_chars, overlap_chars=overlap_chars
            )

        # 把每个 block 封装成 Chunk 对象
        for b in blocks:
            chunks.append(
                Chunk(
                    text=b,
                    source=source,
                    heading_path=path,
                    chunk_index=idx,
                )
            )
            idx += 1

    return chunks


if __name__ == "__main__":
    # 测试代码入口
    # 从 repo 根目录解析路径，这样 PyCharm 或 PowerShell 的当前工作目录 (CWD) 就不会影响结果
    # Path(__file__) 是当前文件路径，.resolve() 获取绝对路径，.parents[1] 返回上两级目录（即 repo 根目录）
    root = Path(__file__).resolve().parents[1]

    # 拼接路径：根目录 / personal / peewee-notes.md
    path = root / "personal" / "peewee-notes.md"

    # 读取文件内容，指定 utf-8 编码
    raw = path.read_text(encoding="utf-8")

    # 调用分块函数，source 传入相对于根目录的相对路径
    chunks = chunk_markdown(raw, source=str(path.relative_to(root)))

    # 打印源文件名和分块总数
    print(f"source={path.name} chunks={len(chunks)}")

    # 只打印前 5 个块的内容摘要
    for c in chunks[:5]:
        print("-" * 40)
        # 打印块索引、标题路径、文本长度
        print(f"[{c.chunk_index}] path={c.heading_path!r} len={len(c.text)}")
        # 打印块的前 120 个字符，把换行替换成 " / " 方便单行显示
        print(c.text[:120].replace("\n", " / "))