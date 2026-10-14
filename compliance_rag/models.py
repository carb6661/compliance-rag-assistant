from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

LEVELS = ["第一级", "第二级", "第三级", "第四级", "第五级"]
CLASSES = ["安全物理环境", "安全通信网络", "安全区域边界", "安全计算环境", "安全管理中心",
           "安全管理制度", "安全管理机构", "安全管理人员", "安全建设管理", "安全运维管理"]
MAX_BYTES = 1_048_576
MAX_FILES = 32
MAX_CHUNKS = 10_000
PARSER_VERSION = "0.1.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class StandardInfo(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=128)
    edition: str = Field(min_length=1, max_length=128)
    synthetic: bool = Field(default=True, strict=True)

    @field_validator("name", "code", "edition")
    @classmethod
    def text_valid(cls, value):
        if not value.strip() or not value.isprintable():
            raise ValueError("standard identity must be nonblank and printable")
        return value


class IngestConfig(StrictModel):
    standards: dict[str, StandardInfo] = Field(default_factory=lambda: {
        "教学等保结构样例": StandardInfo(name="教学等保结构样例", code="DEMO", edition="synthetic-v1")})
    level_aliases: dict[str, str] = Field(default_factory=lambda: {
        **{level: level for level in LEVELS},
        **{level + "安全要求": level for level in LEVELS},
        **{f"第{i}级安全要求": level for i, level in enumerate(LEVELS, 1)}})
    security_classes: list[str] = Field(default_factory=lambda: list(CLASSES))
    requirement_types: list[str] = Field(default_factory=lambda: [
        "安全通用要求", "云计算安全扩展要求", "移动互联安全扩展要求",
        "物联网安全扩展要求", "工业控制系统安全扩展要求"])
    excluded_headings: list[str] = Field(default_factory=lambda: ["前言", "引言", "目录", "参考文献"])
    max_chars: int = Field(default=1200, ge=64, le=20_000, strict=True)

    @field_validator("standards")
    @classmethod
    def standards_valid(cls, value):
        if not value or len(value) > 32 or any(not k.strip() or len(k) > 256 for k in value):
            raise ValueError("expected 1..32 nonempty standard heading aliases")
        return value

    @field_validator("level_aliases")
    @classmethod
    def aliases_valid(cls, value):
        if len(value) > 64 or any(not k.strip() or len(k) > 256 or v not in LEVELS for k, v in value.items()):
            raise ValueError("level alias must map to one of five canonical levels")
        return value

    @field_validator("security_classes", "requirement_types", "excluded_headings")
    @classmethod
    def titles_valid(cls, value):
        if len(value) > 64 or len(set(value)) != len(value) or any(not s.strip() or len(s) > 256 for s in value):
            raise ValueError("expected unique nonblank heading labels, at most 64")
        return value


class Diagnostic(StrictModel):
    severity: Literal["info", "warning", "error"]
    code: str
    source: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    message: str


class ChunkMetadata(StrictModel):
    standard_name: str = Field(alias="标准名称", min_length=1)
    standard_code: str = Field(alias="标准编号", min_length=1)
    edition: str = Field(alias="标准版本", min_length=1)
    security_level: Literal["第一级", "第二级", "第三级", "第四级", "第五级"] = Field(alias="安全级别")
    security_class: str = Field(alias="安全类", min_length=1)
    requirement_type: str = Field(alias="要求类型")
    clause_title: str = Field(alias="条款标题")
    heading_path: list[str] = Field(alias="标题路径")
    source: str = Field(alias="来源文件")
    start_line: int = Field(alias="起始行", ge=1)
    end_line: int = Field(alias="结束行", ge=1)
    start_char: int = Field(alias="起始字符", ge=0)
    end_char: int = Field(alias="结束字符", ge=1)
    section_start_line: int = Field(alias="所属标题行", ge=1)
    source_hash: str = Field(alias="源文件SHA256", pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(alias="内容SHA256", pattern=r"^[0-9a-f]{64}$")
    config_hash: str = Field(alias="配置SHA256", pattern=r"^[0-9a-f]{64}$")
    chunk_id: str = Field(alias="块ID", pattern=r"^[0-9a-f]{64}$")
    child_index: int = Field(alias="子块序号", ge=1)
    synthetic: bool = Field(alias="是否教学示例")
    trust: Literal["unverified"] = Field(default="unverified", alias="来源信任")
    parser_version: str = Field(default=PARSER_VERSION, alias="解析器版本")
