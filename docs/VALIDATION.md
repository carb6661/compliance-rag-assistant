# 验证记录

## 0.2：持久知识库、真实向量与引用问答（2026-10-23）

Windows / Python3.12.8。基础依赖和可选语义依赖分别执行pip check，均无冲突。基础pytest **131项通过**，包含原59项解析回归和新增72项持久化/检索/生成契约/输入/引用/故障测试，没有把Mock当真实LLM。

已执行：

```bash
python -X utf8 -m pytest -q
python -X utf8 -m compliance_rag.live_smoke
python -X utf8 -m compliance_rag.rag_cli evaluate
python -X utf8 -m compliance_rag.semantic_smoke --model-path "<prepared-model-dir>"
```

- 真实Uvicorn回环HTTP：导入/问答/trace/删除/再问答五次均HTTP200；6块导入后返回v2第三级“180天”，删除后业务status=refused/reason=no_evidence。业务拒答不是HTTP故障。
- 真实BGE-small-zh本地ONNX：512维；在禁止socket连接/DNS条件下加载并执行20问，超512 token输入拒绝，未执行生成模型调用。
- 索引模型身份：`BAAI/bge-small-zh-v1.5:fastembed-0.9.0:847676ef008f88641a491b50b92b086472e25adcc7b047a7e5a62fbfcc06c173`。摘要绑定模型/分词器实际文件，不是真实性签名。
- 公开模型固定HF提交46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59；权重未提交GitHub，源码/环境无真实LLM密钥。
- 事务替换/级联删除/回滚、8个相同并发导入仅1次新增修订、模型生成期间删除后的knowledge_changed、trace保留上限、假引用/重复quote/超大响应/超时/401/429/重定向等覆盖见tests/test_rag.py。

### 实际开发集结果

数据为两份原创虚构手册、12块、20问（16个可回答/4个不可回答）。每组问题显式带版本/级别；部分带类别，所以它是较容易的开发验证，不是独立泛化评估。

| 检索模式 | Hit@3 | MRR@3 | 不可回答拒答率 | 越范围hit |
| --- | --- | --- | --- | --- |
| BM25 | 16/16 | 0.96875 | 3/4 | 0 |
| Dense（真实BGE） | 16/16 | 1.0 | 3/4 | 0 |
| Hybrid（RRF） | 16/16 | 0.96875 | 3/4 | 0 |

未加范围过滤的同一BM25基线在16题top3里产生38个越版本/级别hit；过滤后为0。过高0.72阈值曾让Dense仅1/16；开发阈值0.50后16/16。语义分数没有变，只改变门槛；这就是阈值修正，不是模型训练提升。代价是Dense不可回答拒答从4/4降到3/4，不是无代价提升；必须用独立数据重新校准。

[修正前完整记录](../examples/evaluation/semantic-before.json) · [修正后完整记录](../examples/evaluation/semantic-after.json)。每个返回quote都用原文字符切片重验；无引用的拒答项不计作“模型引用正确”。P95为单机暖模型20次查询的一次nearest-rank观察（包含检索/抽取/trace，不含初始化或外部LLM），不是压测/SLA，不作为简历性能承诺。

失败明确保留：`related-unsupported`询问资料没有的“进入记录加密算法”，三模式均可返回相关记录摘录，而不能回答算法。这说明引用精确不是语义蕴含证明。Hybrid没有胜过Dense，也未证明优于BM25，不宣传虚构提升。

### 尚未验证

没有真实生成服务配置/密钥，**未做真实LLM端到端、模型事实质量、费用或服务商兼容性认证**。只有MockTransport契约测试，不把它算模型实测。真实语义可选依赖只在Windows/Python3.12.8运行；常规CI不下载权重。

[0.2首发CI](https://github.com/carb6661/compliance-rag-assistant/actions/runs/39980575362)对应提交`757c806a9af023704c14604d5e2135c57697e9b7`：Windows/Linux × Python3.11/3.12/3.13六组均成功，包含基础131项、离线20问评测与真实本地HTTP。CI不下载可选模型，也不调用真实生成服务；真实BGE是上文单独执行的Windows本地验证，不把两种范围混为一谈。

### 公开内容检查

按拟发布源码/文档范围运行现有Local Secret Scanner，明确排除 `.venv-semantic`、`.models`、`output` 等不发布目录，结果0条规则匹配；人工复核Git拟提交列表没有密钥、私有绝对路径、SQLite或模型权重。全工作区扫描若纳入第三方虚拟环境会产生大量示例/运行变量匹配，不能据此把它们算作本项目公开文件发现。0匹配也不等于绝对无秘密证明。

Workflow Trust Auditor对自身CI为0条静态子集匹配；显式contents:read、直接action SHA固定、禁持久checkout凭据，不代表传递依赖或远程源码已审计。

## 0.1 第一阶段历史验证记录

日期：2026-10-14。环境：Windows，Python 3.12.8，依赖版本见 requirements.lock。

## 已执行

- pytest：59 项通过，覆盖层级作用域、编号/别名、Setext、伪标题、原子块、原文切片、行号、摘要、稳定ID、配置校验、CLI门禁、输出保护和API。
- pip check：未发现依赖冲突。
- 合成标准：1个文件，6个Document，0错误、0警告，前言有排除诊断。
- 元数据过滤：第三级 + 安全物理环境，返回1个块，来源行44—46。
- 结构修复对照：缺安全类样本有1个错误；修复后0错误、2个块，两个级别分别定位。
- 实际Uvicorn回环HTTP：GET /health=200；有效样例POST=200/6个块；不完整样例POST=422/零导出。
- 本地HTTP客户端显式 trust_env=False，避免环境代理将回环请求转发；示例客户端已采用同样设置。
- 发布目录敏感信息规则扫描无匹配；不将规则扫描解释为绝对无泄漏。
- 公开内容只含原创合成样本和标准识别配置，不包含真实标准全文或客户资料。

## 证据与边界

证据为 tests/test_ingestion.py、examples/expected/documents.jsonl、examples/invalid/ 与 examples/repaired/。
跨平台结果以 [GitHub Actions](https://github.com/carb6661/compliance-rag-assistant/actions/workflows/tests.yml) 的实际运行记录为准。

未使用真实受限标准全文验证全部格式，未进行生产压测、真实测评或合规判定，未实现嵌入模型/向量库/语义问答。当前只是结构化入库阶段，不将路线规划当作已实现功能。
