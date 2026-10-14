"""Heading-aware, source-faithful ingestion. Never renders or executes Markdown."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from langchain_core.documents import Document
from markdown_it import MarkdownIt

from .models import ChunkMetadata, Diagnostic, IngestConfig, MAX_BYTES, MAX_CHUNKS, PARSER_VERSION


class InputError(ValueError):
    pass


@dataclass
class ParseResult:
    documents: list[Document]
    diagnostics: list[Diagnostic]
    source_hash: str
    config_hash: str

    @property
    def errors(self):
        return sum(d.severity == "error" for d in self.diagnostics)


def normalize_text(text):
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clean_heading(text):
    # Strip a numbered outline prefix, not arbitrary numbers inside titles.
    return re.sub(r"^\s*(?:\d+(?:\.\d+)*[.、．]?|[一二三四五六七八九十]+[、．])\s+", "", text).strip()


def source_label(name):
    if not isinstance(name, str) or not name or len(name) > 256 or any(c in name for c in "\\:\x00\r\n"):
        raise InputError("source label must be a printable relative POSIX path")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not name.isprintable():
        raise InputError("source label must be a relative path without parent traversal")
    return name


def prepare_config(config):
    roles = {}
    def add(title, role, value):
        key = clean_heading(title)
        if not key or key in roles:
            raise InputError("configuration has overlapping normalized heading aliases")
        roles[key] = (role, value)
    for title, info in config.standards.items():
        add(title, "standard", info)
    for title, level in config.level_aliases.items():
        add(title, "level", level)
    for title in config.security_classes:
        add(title, "class", title)
    for title in config.requirement_types:
        add(title, "type", title)
    for title in config.excluded_headings:
        add(title, "exclude", title)
    return roles


def line_offsets(text):
    offsets = [0] + [m.end() for m in re.finditer("\n", text)]
    if offsets[-1] != len(text):
        offsets.append(len(text))
    return offsets


def line_range(offsets, start, end):
    return bisect_right(offsets, start), bisect_right(offsets, max(start, end - 1))


def paragraph_parts(text, start, end, limit):
    while end - start > limit:
        piece = text[start:start + limit]
        matches = list(re.finditer(r"[\n。！？!?；;]\s*|\s+", piece))
        stop = matches[-1].end() if matches and matches[-1].end() >= limit // 3 else limit
        yield start, start + stop
        start += stop
    if start < end:
        yield start, end


def chunk_ranges(text, start, end, blocks, block_starts, limit, warn):
    # Source gaps (blank lines or reference definitions) stay attached; no text search.
    starts = blocks[bisect_left(block_starts, start):bisect_left(block_starts, end)]
    units = []
    for index, (pos, kind) in enumerate(starts):
        left = start if index == 0 else pos
        right = starts[index + 1][0] if index + 1 < len(starts) else end
        if kind == "paragraph_open" and right - left > limit:
            units.extend(paragraph_parts(text, left, right, limit))
        else:
            units.append((left, right))
            if right - left > limit:
                warn(left, right)
    if not units:
        units = [(start, end)]
        if end - start > limit:
            warn(start, end)
    current = None
    for left, right in units:
        if current and right - current[0] <= limit:
            current = (current[0], right)
        else:
            if current:
                yield current
            current = (left, right)
    if current:
        yield current


def parse_markdown(text, source_name="input.md", config=None, raw_hash=None):
    config = config or IngestConfig()
    if not isinstance(config, IngestConfig):
        raise InputError("expected validated IngestConfig")
    source_label(source_name)
    if not isinstance(text, str):
        raise InputError("expected text")
    try:
        original = text.encode("utf-8")
    except UnicodeError:
        raise InputError("invalid Unicode text") from None
    if len(original) > MAX_BYTES or "\x00" in text:
        raise InputError("expected UTF-8 text at most 1 MiB without NUL")
    digest = raw_hash or sha(original)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise InputError("invalid source digest")
    text = normalize_text(text)
    cfg_hash = sha(canonical_json(config.model_dump()).encode("utf-8"))
    offsets = line_offsets(text)
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = md.parse(text)
    blocks = sorted({offsets[t.map[0]]: t.type for t in tokens
                     if t.level == 0 and t.map and t.type != "heading_open"}.items())
    block_starts = [b[0] for b in blocks]
    roles = prepare_config(config)
    headings = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0:
            inline = tokens[index + 1]
            title = "".join(t.content for t in (inline.children or [])
                            if t.type in {"text", "code_inline", "image"})
            headings.append({"depth": int(token.tag[1:]), "title": title.strip(),
                             "start": offsets[token.map[0]], "body": offsets[token.map[1]],
                             "line": token.map[0] + 1})
    documents, diagnostics, stack = [], [], []

    def diagnostic(severity, code, start, end, message):
        first, last = line_range(offsets, start, end)
        diagnostics.append(Diagnostic(severity=severity, code=code, source=source_name,
                                      start_line=first, end_line=last, message=message))

    first_heading = headings[0]["start"] if headings else len(text)
    if text[:first_heading].strip():
        diagnostic("error", "UNSCOPED_BODY", 0, first_heading,
                   "标题前存在未归类正文；请补全标题或置于明确排除的前言下。")
    for index, heading in enumerate(headings):
        while stack and stack[-1]["depth"] >= heading["depth"]:
            stack.pop()
        role, value = roles.get(clean_heading(heading["title"]), ("other", None))
        stack.append({**heading, "role": role, "value": value})
        start = heading["body"]
        end = headings[index + 1]["start"] if index + 1 < len(headings) else len(text)
        if not text[start:end].strip():
            continue
        if any(h["role"] == "exclude" for h in stack):
            diagnostic("info", "EXCLUDED_SECTION", start, end, "明确配置为非条款章节；未进入检索文档。")
            continue
        standard, level, security_class, req_type = None, None, None, ""
        for ancestor in stack:
            kind, val = ancestor["role"], ancestor["value"]
            if kind == "standard":
                standard, level, security_class, req_type = val, None, None, ""
            elif kind == "level":
                level, security_class, req_type = val, None, ""
            elif kind == "type":
                req_type, security_class = val, None
            elif kind == "class":
                security_class = val
        missing = [key for key, val in [("标准名称", standard), ("安全级别", level), ("安全类", security_class)] if not val]
        if missing:
            diagnostic("error", "MISSING_METADATA", start, end, "缺少可信结构字段：" + "、".join(missing))
            continue

        def oversized(left, right):
            diagnostic("warning", "OVERSIZE_ATOMIC", left, right,
                       "表格、列表、代码或其他原子块超出字符目标；保留完整结构，请人工复核。")

        for child, (left, right) in enumerate(chunk_ranges(text, start, end, blocks, block_starts,
                                                         config.max_chars, oversized), 1):
            content = text[left:right]
            if not content.strip():
                continue
            if len(documents) >= MAX_CHUNKS:
                raise InputError("source exceeds 10000 chunks")
            first, last = line_range(offsets, left, right)
            identity = canonical_json([PARSER_VERSION, cfg_hash, source_name, digest,
                                       heading["start"], left, right]).encode("utf-8")
            metadata = ChunkMetadata.model_validate({
                "标准名称": standard.name, "标准编号": standard.code, "标准版本": standard.edition,
                "安全级别": level, "安全类": security_class, "要求类型": req_type,
                "条款标题": heading["title"], "标题路径": [h["title"] for h in stack],
                "来源文件": source_name, "起始行": first, "结束行": last, "起始字符": left,
                "结束字符": right, "所属标题行": heading["line"], "源文件SHA256": digest,
                "内容SHA256": sha(content.encode("utf-8")), "配置SHA256": cfg_hash,
                "块ID": sha(identity), "子块序号": child, "是否教学示例": standard.synthetic})
            documents.append(Document(page_content=content, metadata=metadata.model_dump(by_alias=True)))
    if not documents and not any(d.severity == "error" for d in diagnostics):
        diagnostic("error", "NO_DOCUMENTS", 0, max(len(text), 1), "没有可入库正文；请检查标准映射与章节。")
    return ParseResult(documents, diagnostics, digest, cfg_hash)


def parse_file(path, source_name=None, config=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise InputError("input must be a regular non-symlink file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise InputError("file exceeds 1 MiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InputError("input must be UTF-8") from None
    return parse_markdown(text, source_name or path.name, config, sha(raw))


def embedding_text(document):
    """Opt-in retrieval prefix; page_content remains an exact normalized source slice."""
    m = document.metadata
    return "\n".join([f"标准：{m['标准名称']}；版本：{m['标准版本']}",
                      " > ".join(m["标题路径"]), document.page_content])
