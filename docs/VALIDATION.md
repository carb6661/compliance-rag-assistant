# 验证记录

## 0.5：持久来源授权与证据发布检查（2026-10-29）

Windows/Python3.12.8实际 **280项测试通过**：原227项按新来源权限合同回归，新增53项。基础pip check无冲突，无新增第三方依赖；应用0.5.0、schema3，Document/解析器仍0.1.0。公开样本新增2份/2块原创虚构资料，不含真实标准或组织记录。

- `source_access_demo`与[固定JSON](../examples/evaluation/source-access.json)一致：相同业务scope下Alice仅甲组180天，Bob仅乙组999天；本地selector只收到甲组。生成中经维护接口撤权后，reason=knowledge_changed，答案/引用/检索片段/候选数清空。旧授权标签412，正文更新清空1项读者，新内容重审授权后返回181天，重开SQLite保留授权。
- demo传输为**进程内ASGI**、selector为明确的本地受控替身；不是实际LLM生成、网络压测、生产攻击测试或语义质量评测。
- `live_smoke`实际启动临时Uvicorn回环HTTP。原角色状态401/403/200/403及条件维护428/200/412/412继续通过；新增来源无授权no_evidence→维护者授权evidence→正文替换后no_evidence。使用临时库与运行时随机令牌，固定输出无令牌/摘要。
- BM25/Dense/Hybrid三模式测试验证评分输入只含授权来源，分数/原文/溯源等同于只放授权语料的参考库；独立库的source ETag按设计不同。未授权候选为0且不调用查询嵌入；不是只过滤top_k输出。
- 撤权检查覆盖查询嵌入中、search结束与生成开始之间（selector调用0次）、selector运行中；直接search对无效快照HTTP409且不含旧文本。更新授权的同事务修订及当前读ACL共同验证，不保证最终检查后无发送窗口，也不撤回已传给模型的内容。
- 同ID新凭据不继承，原凭据失效绑定后不匹配；正文/配置变化清空ACL，no-op保留；删除/重建级联、ACL ABA、跨来源/错类标签、8连接同旧标签仅1提交/7冲突、触发器注入故障回滚ACL/修订/ETag均通过。
- 独立schema2夹具12个真实解析合成块，迁移后逐行相同、source ETag及revision不变、ACL默认空；迁移后注入故障则新列/授权表/schema标记一起回滚；8次并发初始化一致、模型不匹配时不迁移。没有升级工作区真实私有库。
- 真实BGE/FastEmbed/512维ONNX在禁止socket.connect/DNS下运行扩展semantic_smoke：新两块在BM25/Dense/Hybrid均仅1个授权候选、只引用teams/a.md；未授权0候选。原20问三模式Hit@3仍16/16、MRR仍0.96875/1/0.96875、不可回答仍3/4，超512 token仍拒绝。可选依赖仅本机Windows/Python3.12.8运行；CI不下载权重，不把Mock或两块授权检查算独立语义评测。

本轮仍无真实生成服务联调，不尝试未授权付费服务；没有IdP、令牌在线撤销、租户命名空间/段落权限、操作者审计、硬任务截止或持久额度。0.4整库查询风险来自原代码分析，未制造旧版联网利用或虚构攻击成功率。CI及公开文件检查在发布后补充可核验记录。

发布前对66个已跟踪/新公开候选文件逐文件运行Local Secret Scanner，**6处规则匹配**：三个客户端从环境读取本地API令牌、服务端随机生成令牌、测试辅助文件的明确合成常量、拒绝URL凭据测试的合成字符串构造。逐项人工核对无真实凭据字面量，不修改变量名躲规则、不写成0匹配。Git范围无本地令牌/access.json/SQLite/模型权重/虚拟环境，公开示例无真实个人或客户资料。Workflow Trust Auditor对自身CI为0条静态子集发现；这些不构成绝对无秘密或完整安全审计证明。

## 0.4：共享知识库访问与任务准入（2026-10-27）

Windows/Python3.12.8实际 **227项测试通过**：原167项更新为显式测试令牌，新增60项身份/角色/额度/取消/异常/配置/固定实验回归。未修改解析器版本或schema2；基础依赖无新增，pip check无冲突。

- `access_demo`运行临时SQLite与实际RAG路由，输出与[固定证据](../examples/evaluation/access-admission.json)逐字段一致。随机令牌仅内存保留，不进入报告。ASGI客户端与受控selector是本地测试替身，没有真实生成服务。
- 未认证读库401、reader删除403、reader抽取200、reader llm403；拒绝写入前后revision保持不变。另在有可用生成器时验证reader不会调用它。
- 一次analyst工作持槽时，另外7个同身份请求均429；另一个maintainer的llm因生成容量429，而reader本地抽取200。最终实际selector调用1次，任务槽归0。是有限并发功能实验，不是压测、性能或防攻击成功率。
- 真正的ASGI协程取消配合AnyIO `abandon_on_cancel=True`工作线程，HTTP已经结束时仍保留全局及生成槽；worker退出才释放。还验证先取消后worker启动禁止执行、重复close不重复减计数、worker异常/正文413/校验422不会泄漏名额。
- 32并发争抢频率额度5，恰好5个获准/27个429；受控单调时钟验证60秒窗口边界与Retry-After取整。拒绝请求不创建任意IP计数项，维护状态限定于已配置身份。
- 真实Uvicorn回环HTTP继续验证原导入/问答/trace/删除/拒答均200、ETag条件 `[428,200,412,412]`，新增认证/角色状态 `[401,403,200,403]`。使用运行时随机凭据，输出不含令牌。
- 旧条件写实验、离线20问评测和原167项回归继续通过；没有改BGE适配/阈值，本轮未重新测真实BGE，不把历史模型验证写成新测量。真实LLM未联调，不调用未授权付费服务。

当前结果只证明单进程合同。没有文档ACL/租户隔离、在线撤销、分布式限流、持久额度、费用预算、硬任务截止或生产身份审计。旧匿名接口风险由旧代码分析得出，未伪造旧版利用结果。

[0.4首发CI](https://github.com/carb6661/compliance-rag-assistant/actions/runs/40521763134)对应代码提交 `7d98ca95248c41561d11904f2aa3e133f82981ab`，Windows/Linux × Python3.11/3.12/3.13六组全部成功，逐job日志各为227项通过、pip check无冲突，并执行离线20问、真实回环HTTP、条件维护及权限/额度实验。CI不下载BGE权重或调用真实生成服务。

发布前对58个已跟踪/新公开候选文件逐文件运行Local Secret Scanner，**5处规则匹配**，人工核查分别是access.py运行时随机令牌赋值、测试辅助文件的明确合成常量、拒绝URL凭据测试中的合成字符串构造，以及两个客户端从环境读取令牌的赋值；没有真实凭据字面量。保留匹配事实，不修改变量名躲避规则，也不把它写成0匹配。Git范围不含本地令牌、access.json、SQLite、模型或虚拟环境；扫描与人工核查不能证明绝对无秘密。Workflow Trust Auditor对更新后的CI为0条静态子集发现，非完整安全审计。

## 0.3：来源条件维护与升级验证（2026-10-25）

Windows/Python3.12.8实际 **167项测试通过**（0.2原131项回归按新写合同迁移，新增36项）；pip check无冲突。

- `python -X utf8 -m compliance_rag.conditional_demo`与[检入证据](../examples/evaluation/conditional-writes.json)逐项一致：缺条件428、重复create412、A更新200/B旧提交412、两类ABA412，8对象/连接只有1提交/7冲突，revision增长1。
- 测试实际启动4个独立CLI子进程，使用同一个旧标签写同一SQLite文件：退出码0/3/3/3（顺序不定），只有一次修改，不只是Python进程内互斥锁实验。
- 真实Uvicorn回环HTTP：新条件状态 `[428,200,412,412]`；来源读取核验181天和新ETag；原导入/问答/trace/删除/拒答五项仍均HTTP200。
- 独立schema1夹具包括真实解析的12个合成块。升级前后逐行比较chunks无变化，正文和revision保留；迁移中注入故障后generation列/设置一起回滚，8次并发初始化取得同一库身份。没有迁移/清理工作区的真实私有库。
- 原文哈希相同的内容ABA、删除/重建ABA、配置单独变更、跨来源标签误用、无条件写、歧义/弱/列表/重复HTTP头、预检查后向量计算期间的竞争提交、无关来源更新不改变当前来源表示均有回归。
- 真实BGE semantic_smoke在禁止socket/DNS下再次运行：512维与超512 token拒绝继续成立；原20问三模式结果不变（16题Hit@3全部命中，拒答仍3/4）。本轮没有真实LLM凭据，未尝试付费外部调用，也不把契约Mock计为生成模型验证。
- 公开范围Secret Scanner（排除本地venv/权重/output）0条规则匹配，仍需人工核对提交列表，不把规则0匹配写成绝对安全。

[0.3首发CI](https://github.com/carb6661/compliance-rag-assistant/actions/runs/40202875801)对应代码提交`b0a5784924426940fa28326cb1bf6529d59e943b`，Windows/Linux × Python3.11/3.12/3.13六组全部成功，每组实际167项通过、pip check无冲突，均运行离线评测、真实HTTP与条件写演示。CI不下载真实BGE权重或调用真实LLM；真实BGE单独在本机重跑。

按Git拟发布的52个受跟踪文件逐文件扫描为0条规则匹配；`.models`、venv、output未被跟踪。Workflow Trust Auditor对自身CI为0条静态子集匹配，这些都不等于安全证明。0.2旧版最后写者风险基于原代码分析，不宣称做过旧版HTTP利用或生产负载对照。

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
