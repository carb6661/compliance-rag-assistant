# 智能安全合规与等保测评助手 · Compliance RAG Assistant

[![Tests](https://github.com/carb6661/compliance-rag-assistant/actions/workflows/tests.yml/badge.svg)](https://github.com/carb6661/compliance-rag-assistant/actions/workflows/tests.yml)
[![MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**0.3：带条件知识维护的合规资料证据问答。保留0.2真实向量与精确引用，不作自动测评判定。**

本轮继续深化同一RAG主项目：处理“两份旧草稿互相覆盖”和“用旧版本误删新资料”。新建必须create-only，修改/删除必须带来源ETag；提交时在SQLite事务内复核，覆盖正文和删除重建两种ABA都不能复活旧token。详见[问题、设计与升级合同](docs/CONDITIONAL_WRITES.md)。这是写入行为变更，旧客户端应先按指南迁移，原文档解析接口保持兼容。

使用 Python、FastAPI、SQLite 和 LangChain Document，在原有标题层级入库上补齐资料导入、持久查询、更新/删除、范围过滤、BM25/真实中文向量/RRF检索、证据回答、引用校验与追踪。适合安全工程师先定位资料依据、再人工核验的私有本地工作流。

默认BM25和原文抽取无需模型密钥；可选本地512维中文BGE和服务端环境变量配置的OpenAI兼容生成服务。真实BGE已运行，真实LLM服务尚未配置联调，不将适配层Mock当成真实模型结果。解析过程不执行文档指令、不渲染HTML、不抓取链接、不读取API请求指定的服务器文件。公开样本均原创合成，**不是等保标准条文，不能作为测评依据**。

## 本次做深了什么

- 入库不再结束于JSONL：SQLite保存来源、规范化原文、块与可选向量；同源事务替换和级联删除，重启可查。
- 不靠大模型猜适用范围：标准编号、版本和安全级别必填，先过滤再计算检索分数。
- 不盲信生成答案：模型只选连续原文，服务端核对citation ID/quote/区间，生成期间知识变更则拒绝旧答案。
- 不用随机向量冒充语义：真实运行BGE-small-zh ONNX，模型及分词器摘要绑定索引，超512 token拒绝静默截断。
- 不只展示成功：20问开发集保留“相关但缺少事实”的拒答失败，混合检索也没有被包装成必然优于Dense。

详细入口：[实际运行与模型接入](docs/RAG_GUIDE.md) · [项目背景/设计/难点/亮点/简历模板](docs/PROJECT_STORY.md) · [条件维护证据](examples/evaluation/conditional-writes.json) · [真实向量评测](examples/evaluation/semantic-after.json) · [验证记录](docs/VALIDATION.md)

### 先验证一个完整业务流程

安装下面第1节的基础依赖后：

```bash
python -X utf8 -m compliance_rag.rag_cli evaluate
python -X utf8 -m compliance_rag.live_smoke
python -X utf8 -m compliance_rag.conditional_demo
python -m uvicorn compliance_rag.api:app --host 127.0.0.1 --port 8000
```

先选择新的私有演示数据库，再启动服务；另开终端：`python -X utf8 examples/rag_api_client.py`。它只创建两份样本，已有来源即停止且不覆盖。查询v2第三级进入记录返回含“180天”的原文与来源区间，数字只是虚构练习。真实向量与LLM接入见使用指南。

### 已测结果与没有解决的问题

在12块原创资料、20个手写开发问题上（16可回答、4不可回答；非独立测试集）：

| 模式 | Hit@3（16题） | MRR@3 | 不可回答拒答（4题） |
| --- | --- | --- | --- |
| BM25 | 16/16 | 0.96875 | 3/4 |
| BGE Dense（真实本地模型） | 16/16 | 1.0 | 3/4 |
| RRF Hybrid | 16/16 | 0.96875 | 3/4 |

过高0.72阈值曾使Dense仅命中1/16，调整开发阈值0.50后恢复；[修正前记录](examples/evaluation/semantic-before.json)保留原始分数。未过滤的BM25基线有38个越版本/级别hit，正确过滤后为0。以上不是生产准确率、真实LLM答案质量或“消除幻觉”的证明；拒答仍有反例，详见项目复盘。

## 为什么有实用价值

等保知识入库的常见错误不是“没有切成足够小的块”，而是把第二级条款带到第三级、把不同安全类混在一起，或回答时无法回到原文。此模块优先解决这些问题：

| 实际问题 | 本项目处理 | 可核验证据 |
| --- | --- | --- |
| 不同资料标题深度不同 | 语义别名 + 标题栈，不固定 H3 必须是安全类 | 任意标题深度、编号及 Setext 标题测试 |
| 级别切换后误继承 | 同级出栈，标准/级别变化时重置下级字段 | 缺失安全类的错误样本及修复样本 |
| 同名条款被合并 | 标题位置、源文件及配置摘要参与块 ID | 第二级/第三级同名条款测试 |
| 切分后找不到原文 | 字符区间、行号、源码摘要和内容摘要 | 每个块都做源码切片比对 |
| 表格或围栏被硬切 | 以 Markdown 块为原子，超长保留并警告 | 超长表格、列表和代码测试 |
| 有一部分错误仍被当成入库成功 | 严格模式阻止整体成功；部分模式显式标注 | CLI/API 门禁测试 |

可用于：知识库入库预检、标准目录结构修复、向量化前准备、按级别/安全类过滤、检索结果引用。**不能替代标准合法获取、原文审校、人工测评或法律判断。**

## 1. 安装与快速运行

Python 3.11—3.13；推荐独立虚拟环境。安装依赖需要网络，入库不调用外部服务。

```bash
git clone https://github.com/carb6661/compliance-rag-assistant.git
cd compliance-rag-assistant
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
```

macOS/Linux：

```bash
source .venv/bin/activate
python -m pip install -r requirements.lock
```

若不便激活，Windows 使用 .venv/Scripts/python.exe，macOS/Linux 使用 .venv/bin/python。requirements.lock 固定本次测试版本，requirements.in 和 pyproject.toml 保留版本范围；升级应重新验证，不盲目覆盖锁文件。

解析合成样例：

```bash
python -X utf8 -m compliance_rag parse examples/standards --out output/demo.jsonl --diagnostics output/diagnostics.json
python -X utf8 -m pytest -q
```

预期：1 个文件，6 个可入库块，0 个错误，0 个警告；前言被明确排除并留下 info 诊断。完整预期结果见 [documents.jsonl](examples/expected/documents.jsonl)。

重复写同一输出默认拒绝；需要覆盖自己已确认的报告时添加 --force。output/ 与 private-docs/ 被 Git 忽略，但忽略规则不是访问控制。

## 2. 真实标准如何接入

1. 自行合法取得并核对 Markdown；不要将受限标准全文、客户资料或测评证据上传公开仓库。
2. 复制 [真实标准配置模板](examples/real-standard-config.template.json)，保存到 private-docs/config.json。
3. 将 standards 中的键改成 Markdown 实际标准标题；name/code/edition 填经核对的名称、编号和版本，真实材料设置 synthetic=false。
4. 确认级别、要求类型和安全类标题的实际写法；需要时配置 level_aliases、security_classes、requirement_types。
5. 先只检查，不输出入库文件：

```bash
python -X utf8 -m compliance_rag parse private-docs --config private-docs/config.json
```

6. 逐项修复诊断，再导出：

```bash
python -X utf8 -m compliance_rag parse private-docs --config private-docs/config.json --out output/real-documents.jsonl --diagnostics output/real-diagnostics.json
```

目录递归处理所有 .md 文件，最多 32 个；请使用专门的标准目录，不把普通 README 混进去。解析器不根据正文猜测安全级别，不根据文件名猜测标准版本，也不默认所有内容都是“安全通用要求”。

[国家标准公开页面](https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=BAFB47E8874764186BDB7865E8344DAF)列出 GB/T 22239-2019《信息安全技术 网络安全等级保护基本要求》。模板只示范元数据映射，不转载条文，也不证明任何本地文件等同于官方原本；具体适用版本应另行核实。

更多配置及限制见[入库说明](docs/INGESTION.md)。

## 3. Document 与元数据

输出每行一个 LangChain 兼容对象：

```json
{
  "page_content": "原创教学正文……",
  "metadata": {
    "标准名称": "教学等保结构样例",
    "标准编号": "DEMO",
    "标准版本": "synthetic-v1",
    "安全级别": "第三级",
    "安全类": "安全物理环境",
    "要求类型": "安全通用要求",
    "条款标题": "8.1.1.1 进入记录（原创练习）",
    "标题路径": ["教学等保结构样例", "8 第三级安全要求", "..."],
    "来源文件": "synthetic-standard.md",
    "起始行": 43,
    "结束行": 44,
    "起始字符": 0,
    "结束字符": 10,
    "源文件SHA256": "...",
    "内容SHA256": "...",
    "配置SHA256": "...",
    "块ID": "...",
    "子块序号": 1,
    "是否教学示例": true,
    "来源信任": "unverified",
    "解析器版本": "0.1.0"
  }
}
```

上面仅为字段说明，省略号和位置数字不是可验证样例；实际值请查看检入的 JSONL。所有输出块都经 Pydantic 检查必填标准、级别及安全类；无法确定的正文进入诊断而不伪造字段。

源码先移除开头 BOM 并将 CRLF/CR 统一成 LF。字符区间为**规范化文本的 Python Unicode 字符索引 [start, end)**，行号从 1 起且首末均包含。page_content 是该区间的原样切片，不额外加入父标题；源文件 SHA-256 则对应原始文件字节。因此“规范化切片”和“原始字节摘要”用途不同。

标题路径包含原始标题文字及编号，所属标题行可定位其上下文。长段落的子块有各自精确位置；同一原子块过长则保持完整。块 ID 绑定解析器版本、配置、来源标签、原始文件摘要及位置，不是跨版本的语义去重 ID。

## 4. 接入现有 RAG

Python 中直接取得 Document：

```python
from compliance_rag.parser import parse_file, embedding_text

result = parse_file("examples/standards/synthetic-standard.md")
if result.errors:
    raise RuntimeError("先修复诊断，不要将不完整批次送入向量库")

documents = result.documents
# 你的向量化流程可以使用：
texts = [embedding_text(doc) for doc in documents]
metadatas = [doc.metadata for doc in documents]
```

embedding_text 显式加入标准、版本和标题路径，避免外接向量化时丢失父标题。它不修改原始 page_content。0.2另提供持久知识库、本地BGE及证据选摘流程，见 [RAG使用指南](docs/RAG_GUIDE.md)；本节仍保留第一阶段对外Document接口。

不接任何外部模型也可验证元数据过滤：

```bash
python examples/filter_chunks.py output/demo.jsonl --level 第三级 --security-class 安全物理环境
```

预期找到 1 个块并显示来源区间。这是精确字段过滤演示，不是语义检索，也不是等保结论。

## 5. FastAPI 本地接口

```bash
python -m uvicorn compliance_rag.api:app --host 127.0.0.1 --port 8000
```

- GET /health：版本与阶段。
- POST /v1/parse：接收 text、source_name、config 和 mode（strict/partial）。
- GET /docs：接口文档；无需前端或付费服务。

另开终端（原解析API兼容示例）：

```bash
python examples/api_client.py
```

API 使用同一解析模块。严格模式有结构错误返回 422 且 documents=[]；显式 partial 模式返回 200/status=partial，调用方必须检查 status，不能只检查 HTTP 成功。请求模式错误返回 422，非 JSON 返回 415，超过 2 MiB 请求体返回 413；文本本身还限制 UTF-8 1 MiB。

API不接受文件路径、远程URL或任意上传保存位置，不提供抓取功能。来源名称仅作相对标签，不会用于读文件。0.2新增 /v1/library、/v1/search、/v1/ask、/v1/traces，详见使用指南。**已加入单用户SQLite持久化，但仍无用户鉴权、租户隔离或并发配额；只绑定回环地址，不得直接暴露公网。** TrustedHost、版本过滤和大小限制都不能代替对象授权。

## 6. 修复前后实验

```bash
python -X utf8 -m compliance_rag parse examples/invalid/missing-class.md --out output/blocked.jsonl
python -X utf8 -m compliance_rag parse examples/repaired/missing-class-fixed.md --out output/repaired.jsonl
```

| 输入 | 实际预期 | 为什么 |
| --- | --- | --- |
| 缺少安全类的原始样本 | 返回 2，严格模式不创建新导出 | 第三级不能沿用第二级已退出作用域的类别 |
| 补充安全类的修复样本 | 返回 0，导出 2 块 | 两个级别各有明确安全类 |

这里修复的是**文档结构**，不是宣称修复了真实系统的安全控制。若已有旧输出，严格失败不会删除它；必须以本次状态和摘要判断，不把旧文件误认新结果。

默认严格是建议入库流程。--allow-partial 只导出已校验的有效块并返回 1；元数据不完整的部分依然不导出，也不能据部分结果宣称整个标准已覆盖。

## 7. 安全边界与限制

- 来源信任始终 unverified；哈希不证明标准真实、完整、有许可或适用。
- 文档中的命令、代码和链接作为数据保留，不执行。它们仍可能影响未来的大模型，本模块**不是提示词注入防火墙**。
- 标题结构错误、遗漏条文、图片内文字、OCR错误、未配置别名或不规范 Markdown 需要人工处理。
- 标准标题、级别及类别依赖显式配置和层级，不能保证原文语义正确；只有来源审核能进一步提高可信性。
- 暂不处理PDF、Word、跨文件标题继承或自动合规判断；0.2已补本地向量、检索与引用问答，但生成质量仍需真实服务及独立数据验证。
- 字符目标不是 token 上限；超长表格/列表/围栏可能超过目标，需要按下游模型容量复核。
- 每文件 1 MiB，每批最多32个文件及10,000个块；API请求体2 MiB。这些是保护上限，不是实测性能承诺。
- JSONL 与诊断为文件级原子写入，不是多文件事务。使用本地支持硬链接的文件系统；IO失败时检查各输出状态。
- 真实文件及结果可能含受限内容；不自动脱敏，不上传客户测评资料。

详细[威胁模型](docs/THREAT_MODEL.md) · [安全使用](SECURITY.md) · [实际验证记录](docs/VALIDATION.md)。

## 8. 工程结构与岗位能力

```text
compliance_rag/
  models.py       配置与中文元数据验证
  parser.py       语法树、标题栈、原文区间与Document
  __main__.py     批处理、入库门禁、JSONL与原子输出
  api.py          本地FastAPI与有界/无歧义JSON请求
  knowledge.py    SQLite事务来源/块/向量存储与范围过滤
  conditions.py   来源条件、严格If-Match/If-None-Match合同
  conditional_demo.py  旧草稿/ABA/并发写真实合成验证
  retrieval.py    中文词项BM25、余弦、RRF
  embeddings.py   可选真实中文BGE、文件摘要、token门禁
  generation.py   兼容模型、原文引用验证、revision复核、trace
  rag_api.py      知识库/检索/问答/追踪接口
  rag_cli.py      持久知识库CLI与评测入口
  evaluation.py   20问合成对照评测
examples/
  standards/      原创合成标准
  invalid/        结构缺陷样例
  repaired/       结构修复样例
  expected/       精确可重现输出
tests/            溯源、作用域、切分、CLI和API测试
docs/             使用、威胁模型及验证记录
```

可展示安全知识工程、RAG数据准备、数据来源治理、输入校验、API边界和自动化回归能力；不虚构真实测评经验或企业部署。面试可解释：为什么不能固定每一层的语义、为何默认拒绝不完整入库、如何验证源码位置、以及切分正确为什么不等于结论正确。

许可证 MIT，仅覆盖本项目原创代码、文档和合成样本；不自动覆盖用户导入的标准或第三方资料。
