import hashlib
import json
import re
import socket
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
from langchain_core.documents import Document
from pydantic import ValidationError
import pytest

from compliance_rag.api import create_app
from access_support import HEADERS, make_policy
from compliance_rag.models import IngestConfig, MAX_BYTES
from compliance_rag.parser import InputError, embedding_text, normalize_text, parse_file, parse_markdown
from compliance_rag.__main__ import atomic_write, discover, load_config

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples/standards/synthetic-standard.md"
BROKEN = ROOT / "examples/invalid/missing-class.md"
PREFIX = "# 教学等保结构样例\n\n## 第二级安全要求\n\n### 安全物理环境\n\n"
app = create_app(make_policy())
client = TestClient(app, headers=HEADERS)


def check_slices(result, text):
    text = normalize_text(text)
    for doc in result.documents:
        m = doc.metadata
        assert doc.page_content == text[m["起始字符"]:m["结束字符"]]
        assert m["起始行"] == text[:m["起始字符"]].count("\n") + 1
        assert m["结束行"] == text[:m["结束字符"]-1].count("\n") + 1
        assert m["内容SHA256"] == hashlib.sha256(doc.page_content.encode()).hexdigest()


def test_synthetic_fixture_metadata_and_slices():
    result = parse_file(SAMPLE)
    assert result.errors == 0
    assert len(result.documents) == 6
    assert all(isinstance(d, Document) for d in result.documents)
    assert {d.metadata["安全级别"] for d in result.documents} == {"第二级", "第三级"}
    assert {d.metadata["安全类"] for d in result.documents} == {
        "安全物理环境", "安全通信网络", "安全计算环境", "安全管理中心"}
    assert all(d.metadata["来源信任"] == "unverified" for d in result.documents)
    assert all(d.metadata["是否教学示例"] for d in result.documents)
    check_slices(result, SAMPLE.read_text(encoding="utf-8"))


def test_golden_documents_are_reproducible():
    actual = parse_file(SAMPLE).documents
    expected = [json.loads(line) for line in (ROOT / "examples/expected/documents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [{"page_content":d.page_content, "metadata":d.metadata} for d in actual] == expected


def test_structure_repair_before_and_after():
    assert parse_file(BROKEN).errors == 1
    result = parse_file(ROOT / "examples/repaired/missing-class-fixed.md")
    assert result.errors == 0
    assert len(result.documents) == 2
    assert [d.metadata["安全级别"] for d in result.documents] == ["第二级", "第三级"]


def test_type_inheritance_and_reset():
    docs = parse_file(SAMPLE).documents
    assert docs[-1].metadata["要求类型"] == "云计算安全扩展要求"
    assert all(d.metadata["要求类型"] == "安全通用要求" for d in docs[:-1])


def test_repeated_clause_titles_do_not_merge():
    docs = parse_file(SAMPLE).documents
    targets = [d for d in docs if "进入记录" in d.metadata["条款标题"]]
    assert len(targets) == 2
    assert len({d.metadata["块ID"] for d in targets}) == 2


def test_missing_class_after_level_switch_not_inherited():
    result = parse_file(BROKEN)
    assert len(result.documents) == 1
    assert result.errors == 1
    assert "安全类" in result.diagnostics[-1].message
    assert result.documents[0].metadata["安全级别"] == "第二级"


def test_class_sibling_unknown_is_blocked():
    result = parse_markdown(PREFIX + "有效。\n\n### 未配置类别\n\n不得沿用旧类别。")
    assert len(result.documents) == 1
    assert result.errors == 1


def test_new_standard_clears_old_fields():
    text = PREFIX + "有效。\n\n# 其他未配置标准\n\n## 第三级安全要求\n\n### 安全物理环境\n\n不应归旧标准。"
    result = parse_markdown(text)
    assert len(result.documents) == 1
    assert result.errors == 1
    assert "标准名称" in result.diagnostics[-1].message


def test_nested_standard_resets_level():
    cfg = IngestConfig.model_validate({"standards": {
        "教学等保结构样例": {"name":"A", "code":"A", "edition":"v1"},
        "另一个标准": {"name":"B", "code":"B", "edition":"v1"}}})
    result = parse_markdown(PREFIX + "有效。\n\n#### 另一个标准\n\n新标准不得继承旧级别。", config=cfg)
    assert len(result.documents) == 1
    assert result.errors == 1


@pytest.mark.parametrize("heading", ["第二级", "第二级安全要求", "第2级安全要求", "7. 第二级安全要求", "7.1 第二级安全要求"])
def test_level_aliases_and_number_prefix(heading):
    result = parse_markdown(PREFIX.replace("第二级安全要求", heading) + "原创正文。")
    assert result.errors == 0
    assert result.documents[0].metadata["安全级别"] == "第二级"


def test_arbitrary_heading_depth():
    text = "# 合集\n\n## 教学等保结构样例\n\n### 第三级安全要求\n\n#### 安全通用要求\n\n##### 安全物理环境\n\n###### 条款\n\n原创正文。"
    result = parse_markdown(text)
    assert result.errors == 0
    assert result.documents[0].metadata["安全类"] == "安全物理环境"


def test_setext_and_inline_heading_text():
    text = "教学等保结构样例\n================\n\n第二级安全要求\n----------------\n\n### **安全物理环境**\n\n正文。"
    result = parse_markdown(text)
    assert result.errors == 0
    check_slices(result, text)


def test_fenced_and_quoted_titles_not_real_headings():
    text = PREFIX + "```markdown\n## 第三级安全要求\n```\n\n> ## 第三级安全要求\n\n仍是第二级。"
    result = parse_markdown(text)
    assert result.errors == 0
    assert len(result.documents) == 1
    assert result.documents[0].metadata["安全级别"] == "第二级"
    assert "```markdown" in result.documents[0].page_content


def test_excluded_sections_are_diagnosed():
    result = parse_markdown(SAMPLE.read_text(encoding="utf-8"))
    assert any(d.code == "EXCLUDED_SECTION" for d in result.diagnostics)
    assert not any("不是国家标准条文" in d.page_content for d in result.documents)


@pytest.mark.parametrize("text", ["", "无标题正文", "# 教学等保结构样例\n\n正文缺少分类。", "# 只有标题"])
def test_empty_unscoped_and_missing_metadata(text):
    assert parse_markdown(text).errors > 0


def test_long_paragraph_exact_coverage_and_bound():
    text = PREFIX + "示例句子需要保留。"*100
    result = parse_markdown(text, config=IngestConfig(max_chars=64))
    assert result.errors == 0
    assert len(result.documents) > 1
    assert all(len(d.page_content) <= 64 for d in result.documents)
    assert "".join(d.page_content for d in result.documents) == text[len(PREFIX)-1:]
    check_slices(result, text)


@pytest.mark.parametrize("body", [
    "|字段|说明|\n|---|---|\n" + "|a|" + "说明"*60 + "|\n",
    "```text\n" + "x"*150 + "\n```\n",
    "- " + "item "*40 + "\n- second\n",
])
def test_oversize_atomic_blocks_are_kept(body):
    result = parse_markdown(PREFIX + body, config=IngestConfig(max_chars=64))
    assert result.errors == 0
    assert len(result.documents) == 1
    assert any(d.code == "OVERSIZE_ATOMIC" for d in result.diagnostics)
    assert body in result.documents[0].page_content


def test_id_stability_and_config_identity():
    text = PREFIX + "正文。"
    first = parse_markdown(text)
    assert first.documents == parse_markdown(text).documents
    changed = parse_markdown(text, config=IngestConfig(max_chars=64))
    assert first.documents[0].metadata["块ID"] != changed.documents[0].metadata["块ID"]
    assert first.documents[0].metadata["块ID"] != parse_markdown(text, "other.md").documents[0].metadata["块ID"]


def test_crlf_bom_and_raw_digest(tmp_path):
    path = tmp_path / "source.md"
    raw = b"\xef\xbb\xbf" + (PREFIX+"正文。").replace("\n","\r\n").encode()
    path.write_bytes(raw)
    result = parse_file(path)
    assert result.source_hash == hashlib.sha256(raw).hexdigest()
    check_slices(result, raw.decode("utf-8-sig"))


def test_embedding_prefix_not_source_mutation():
    doc = parse_markdown(PREFIX + "正文。").documents[0]
    content = doc.page_content
    assert "第二级安全要求" in embedding_text(doc)
    assert doc.page_content == content


@pytest.mark.parametrize("value", ["../secret.md", "/root/a.md", "C:\\a.md", "line\n.md"])
def test_bad_source_labels(value):
    with pytest.raises(InputError):
        parse_markdown(PREFIX+"正文。", value)


@pytest.mark.parametrize("value", ["\x00", "\ud800", "x"*(MAX_BYTES+1)], ids=["nul", "surrogate", "oversize"])
def test_bad_or_large_text(value):
    with pytest.raises(InputError):
        parse_markdown(value)


def test_invalid_utf8_and_limit(tmp_path):
    path = tmp_path / "source.md"
    for raw in [b"\xff", b"x"*(MAX_BYTES+1)]:
        path.write_bytes(raw)
        with pytest.raises(InputError):
            parse_file(path)


@pytest.mark.parametrize("changes", [{"max_chars":63}, {"max_chars":True}, {"unknown":1},
    {"security_classes":["重复","重复"]}, {"level_aliases":{"二级":"不存在"}}])
def test_config_validation(changes):
    with pytest.raises(ValidationError):
        IngestConfig.model_validate(changes)


def test_config_overlap_and_duplicate_json(tmp_path):
    with pytest.raises(InputError):
        parse_markdown(PREFIX+"正文。", config=IngestConfig(security_classes=["第二级"]))
    config = tmp_path / "config.json"
    config.write_text('{"max_chars":64,"max_chars":80}', encoding="utf-8")
    with pytest.raises(InputError):
        load_config(config)


def run_cli(*args):
    return subprocess.run([sys.executable, "-X", "utf8", "-m", "compliance_rag", "parse", *map(str,args)],
                          cwd=ROOT, capture_output=True, encoding="utf-8")


def test_cli_valid_batch_and_file_protection(tmp_path):
    output = tmp_path / "chunks.jsonl"
    result = run_cli(SAMPLE.parent, "--out", output)
    assert result.returncode == 0, result.stderr
    assert len(output.read_text(encoding="utf-8").splitlines()) == 6
    original = output.read_bytes()
    assert run_cli(SAMPLE, "--out", output).returncode == 2
    assert output.read_bytes() == original
    assert run_cli(SAMPLE, "--out", output, "--force").returncode == 0


def test_cli_strict_and_partial(tmp_path):
    output = tmp_path / "chunks.jsonl"
    diagnostics = tmp_path / "diagnostics.json"
    result = run_cli(BROKEN, "--out", output, "--diagnostics", diagnostics)
    assert result.returncode == 2
    assert not output.exists()
    assert json.loads(diagnostics.read_text(encoding="utf-8"))["status"] == "blocked"
    partial = run_cli(BROKEN, "--out", output, "--allow-partial")
    assert partial.returncode == 1
    assert len(output.read_text(encoding="utf-8").splitlines()) == 1


def test_cli_never_overwrites_input_or_colliding_outputs(tmp_path):
    source = tmp_path / "source.md"
    source.write_text(PREFIX+"正文。", encoding="utf-8")
    original = source.read_bytes()
    assert run_cli(source, "--out", source, "--force").returncode == 2
    assert source.read_bytes() == original
    out = tmp_path / "same.json"
    assert run_cli(source, "--out", out, "--diagnostics", out).returncode == 2


def test_atomic_no_replace(tmp_path):
    out = tmp_path / "existing.txt"
    out.write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        atomic_write(out, "new")
    assert out.read_text() == "original"
    assert not list(tmp_path.glob(".ingest-*"))


def test_batch_limit(tmp_path):
    for i in range(33):
        (tmp_path / f"{i}.md").write_text(PREFIX+"正文。", encoding="utf-8")
    with pytest.raises(InputError):
        discover(tmp_path)


def test_health_and_api_success():
    assert client.get("/health").status_code == 200
    response = client.post("/v1/parse", json={"text":SAMPLE.read_text(encoding="utf-8")})
    assert response.status_code == 200
    assert response.json()["accepted_chunks"] == 6


def test_api_strict_and_partial():
    payload = {"text":BROKEN.read_text(encoding="utf-8")}
    strict = client.post("/v1/parse", json=payload)
    assert strict.status_code == 422
    assert strict.json()["documents"] == []
    partial = client.post("/v1/parse", json={**payload,"mode":"partial"})
    assert partial.status_code == 200
    assert partial.json()["status"] == "partial"
    assert len(partial.json()["documents"]) == 1


def test_api_no_paths_urls_or_error_echo():
    for extra in [{"path":"/etc/passwd"},{"url":"https://invalid.example"}]:
        response = client.post("/v1/parse", json={"text":"PRIVATE_MARKER", **extra})
        assert response.status_code == 422
        assert "PRIVATE_MARKER" not in response.text


def test_api_content_type_host_and_body_limit():
    assert client.post("/v1/parse", content="{}").status_code == 415
    assert client.get("/health", headers={"host":"evil.example"}).status_code == 400
    response = client.post("/v1/parse", content=b"x"*(2*MAX_BYTES+1),
                           headers={"content-type":"application/json"})
    assert response.status_code == 413


def test_api_byte_limit_and_surrogate():
    response = client.post("/v1/parse", json={"text":"中"*(MAX_BYTES//2)})
    assert response.status_code == 422
    response = client.post("/v1/parse", content=b'{"text":"\\ud800"}',
                           headers={"content-type":"application/json"})
    assert response.status_code == 422


def test_parsing_without_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("outbound network attempted")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert parse_file(SAMPLE).errors == 0


def test_strict_failure_does_not_replace_previous_export(tmp_path):
    output = tmp_path / "existing.jsonl"
    output.write_text("old", encoding="utf-8")
    assert run_cli(BROKEN, "--out", output, "--force").returncode == 2
    assert output.read_text() == "old"


def test_chunk_limit(monkeypatch):
    monkeypatch.setattr("compliance_rag.parser.MAX_CHUNKS", 1)
    with pytest.raises(InputError):
        parse_file(SAMPLE)


def test_symbolic_file_rejected(monkeypatch):
    monkeypatch.setattr(Path, "is_symlink", lambda path: True)
    with pytest.raises(InputError):
        parse_file(SAMPLE)


def test_filter_example():
    result = subprocess.run([sys.executable, "-X", "utf8", "examples/filter_chunks.py",
        "examples/expected/documents.jsonl", "--level", "第三级", "--security-class", "安全物理环境"],
        cwd=ROOT, capture_output=True, encoding="utf-8")
    assert result.returncode == 0
    assert json.loads(result.stdout)["matches"] == 1


def test_document_links():
    paths = [ROOT/"README.md", ROOT/"SECURITY.md", *(ROOT/"docs").glob("*.md")]
    for path in paths:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("https://","http://","#")):
                continue
            assert (path.parent/target.split("#",1)[0]).exists(), target
