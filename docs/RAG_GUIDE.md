# 实际使用：持久知识库与可核验问答（0.5）

0.2补齐业务闭环，0.3加入来源条件维护，0.4加入令牌角色/单进程额度，0.5加入持久来源读ACL及更新重审。先按[令牌启动](ACCESS_CONTROL.md)配置HTTP，再按[来源权限与schema3升级](SOURCE_AUTHORIZATION.md)审阅授权；写入继续使用[条件合同](CONDITIONAL_WRITES.md)。未配置API503锁定、缺认证401、角色不足403；查询者没有来源授权时返回no_evidence。仍不声称自动完成等保测评。

## 1. 无密钥即可验证业务闭环

先按 README 安装 `requirements.lock`，在仓库根目录运行：

```bash
python -X utf8 -m compliance_rag.rag_cli ingest examples/knowledge/v1.md --source-name knowledge/v1.md --config examples/knowledge/config-v1.json --create-only
python -X utf8 -m compliance_rag.rag_cli ingest examples/knowledge/v2.md --source-name knowledge/v2.md --config examples/knowledge/config-v2.json --create-only
python -X utf8 -m compliance_rag.rag_cli inventory
python -X utf8 -m compliance_rag.rag_cli ask "进入记录保留多久？" --standard-code TRAINING-POLICY --edition v2 --level 第三级 --security-class 安全物理环境
```

默认数据库 `output/knowledge.sqlite3`，重启后仍可查询。两份原创虚构手册共12块；v2第三级摘录含“180天”，v2第二级含“60天”，v1第三级含“90天”。这些数字故意不同，用来验证版本与级别隔离，**不是任何真实标准的要求**。

删除同一个来源后，它的块与向量同事务级联删除：

```bash
python -X utf8 -m compliance_rag.rag_cli source knowledge/v2.md
# 审阅当前来源后，将返回的标签替换下面的占位；不要照抄尖括号
python -X utf8 -m compliance_rag.rag_cli remove knowledge/v2.md --if-match "REPLACE_WITH_REVIEWED_ETAG"
```

再次查询v2应无证据；不会回退到v1回答。持有当前If-Match且内容/配置未变时返回unchanged、不增长修订号且保留ACL；create-only重复创建即412。实际修改正文/配置会清空普通读者授权并返回reader_grants_cleared，须人工审阅后重新授权。陈旧条件在事务内拒绝；解析/向量/事务失败保留旧来源和ACL。没有审批工作流或自动合并；条件写不等于用户权限控制。CLI是可信本地路径，不执行HTTP读ACL。

## 2. API 业务演示

```powershell
python -m compliance_rag.access output/access
$env:RAG_ACCESS_JSON = Get-Content -Raw -Encoding utf8 output/access/access.json
$env:RAG_DB_PATH = 'output/demo-v04.sqlite3'
python -m uvicorn compliance_rag.api:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

output/access必须是全新私有目录；已有令牌应直接加载，勿重复初始化或泄露。另开终端加载维护令牌再运行演示：

```powershell
$env:RAG_API_TOKEN = (Get-Content -Raw -Encoding utf8 output/access/maintainer.token).Trim()
python -X utf8 examples/rag_api_client.py
```

样本已有则停止，不为重复演示删除或覆盖旧数据。Swagger页关闭，维护者可带认证读取/openapi.json。所有业务请求均须Authorization头；下面表格描述的JSON/条件头须在通过身份和角色校验后使用。

这个全流程客户端使用maintainer，所以可查全库。若用reader/analyst查询，先按[来源权限指南](SOURCE_AUTHORIZATION.md)运行manage_source_access.py读取ACL、审阅正文并携带access ETag设置读者；未授权不会因scope匹配而得到证据。不要用维护者权限展示普通用户隔离。

| 方法/路由 | 输入/用途 | 重要边界 |
| --- | --- | --- |
| POST /v1/parse | 保留原始结构解析接口 | 只有严格成功才能作为完整入库候选 |
| POST /v1/library/ingest | text/source_name/config + 条件头 | 新建If-None-Match:*，修改If-Match；事务内比较后才提交 |
| POST /v1/library/source | source_name | 私有规范化原文与source_etag；不存在404，不作任意文件读取 |
| GET /v1/library | 来源、版本、级别/类别清单、修订号 | 共享私有本地知识库，仅maintainer可读全量索引 |
| POST /v1/library/remove | source_name + If-Match | 只删除仍未变化的来源；旧标签412，缺条件428 |
| POST /v1/library/access | source_name | maintainer读取读者/来源状态及独立access_etag |
| POST /v1/library/access/update | source_name/readers + access If-Match | maintainer完整替换读者，限定已配置的查询身份；正文更新后重审 |
| POST /v1/search | question/scope/retrieval/top_k | scope+服务端身份ACL先过滤；快照变化409且无证据 |
| POST /v1/ask | search字段及 generation | extractive或llm；拒答也返回明确业务状态 |
| GET /v1/traces/{trace_id} | 本次trace标识 | 仅maintainer；不保存问题、正文、prompt或密钥 |

请求例子：

```json
{
  "question": "进入记录保留多久？",
  "scope": {
    "standard_code": "TRAINING-POLICY",
    "edition": "v2",
    "security_level": "第三级",
    "security_class": "安全物理环境"
  },
  "retrieval": "bm25",
  "top_k": 3,
  "generation": "extractive"
}
```

`status=evidence`是资料摘录，不是合规判定。`status=refused`检查reason：no_evidence、model_not_configured、model_abstained、invalid_model_citation、invalid_model_selection、knowledge_changed或静态upstream错误。业务拒答HTTP200；结构/索引问题422、存储503、字节/帧预算413；来源条件缺失428、陈旧412、歧义400；认证/角色/额度还可返回401/403/429。不要只判断HTTP成功，写请求不自动重试覆盖。

## 3. 真实本地中文语义检索

BM25是默认可复现基线，不将它或哈希投影冒充语义嵌入。可选真实 BGE-small-zh ONNX（512维）：

```bash
python -m pip install -r requirements-semantic.lock
python -X utf8 -m compliance_rag.rag_cli prepare-model
```

下载公开权重需要网络；代码固定 `Qdrant/bge-small-zh-v1.5` 的提交 `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`，不会自动使用滚动latest。命令返回本机模型目录；以下将其记为 `<model-dir>`，请替换为真实目录，不照抄尖括号。

```bash
python -X utf8 -m compliance_rag.rag_cli evaluate --model-path "<model-dir>" --out output/my-semantic-report.json
python -X utf8 -m compliance_rag.semantic_smoke --model-path "<model-dir>"
```

第二条命令禁止 socket连接/DNS，同时加载模型、运行真实向量和20问评测、检查超512 token拒绝，确保没有在推理过程中上传文档。模型初始化及下载不计入评测P95。可选依赖目前只在Windows/Python3.12.8完成真实运行；常规六矩阵CI不下载这些权重，不声称每个平台都验证过ONNX。

要让API使用真实模型，在**启动服务前**设置 `RAG_EMBEDDING_MODEL_PATH=<model-dir>` 和新的 `RAG_DB_PATH=output/semantic.sqlite3`，重新导入；客户端再运行：

```bash
python -X utf8 examples/rag_api_client.py --retrieval hybrid
```

离线BM25数据库和向量数据库不要混用。数据库绑定模型名称、FastEmbed版本、模型/分词器文件组合摘要和维度；更换模型必须使用新的库并显式重建。文件摘要绑定实际字节，不证明下载来源绝对可信。

本版本对过滤后的最多2000块做精确余弦扫描，不是ANN/HNSW或外部向量数据库。RRF(k=60)融合BM25/向量前50候选；同分用块ID稳定排序，不另冒充cross-encoder。中文采用字符及二字组加英文词的简单分词；不是完整中文分词器。BM25统计也只在过滤后语料计算，防止先混排再过滤导致的候选缺失。

语义阈值0.50是本套开发样本下的启发式，不是答案置信度，也没有独立验证集。`evaluate --dense-min-cosine 0.72` 可重放过高阈值导致的召回下降。不能把这次调参当作普遍最优或上线阈值。

## 4. 接入 OpenAI 兼容生成服务

只在服务端配置。模型选摘会将**问题和最多6000字符的检索原文**发送到配置服务；上传真实资料前须确认许可、服务商数据政策和脱敏规则。

PowerShell 7 示例（密钥仅在本机遮罩输入；不要回传聊天、不要粘入脚本）：

```powershell
$env:RAG_ENABLE_EXTERNAL = '1'
$env:RAG_LLM_BASE_URL = 'https://你的已授权服务域名/v1'
$env:RAG_LLM_MODEL = '服务商实际提供的模型名称'
$env:RAG_LLM_API_KEY = Read-Host '仅在本机输入 API Key' -MaskInput
$env:RAG_ACCESS_JSON = Get-Content -Raw -Encoding utf8 output/access/access.json
python -m uvicorn compliance_rag.api:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

已按第2节初始化访问策略并在另一个终端加载维护令牌后：`python -X utf8 examples/rag_api_client.py --llm`，如服务已启用语义库可再加 `--retrieval hybrid`。一般仅查询的客户端使用analyst令牌才能调用llm；reader请求llm会403。`.env` 被Git忽略但**不自动加载**，操作系统环境变量必须在启动进程前生效。若用旧版PowerShell，使用系统环境变量管理器或你的密钥管理工具，不要明文提交。

客户端无法提交 base_url、api_key、model或数据库路径，这些多余字段会422拒绝。一般外部地址必须HTTPS；HTTP仅允许回环模型服务。禁用重定向和环境代理，连接5秒/读取30秒超时，只尝试一次；返回体最多64KiB，不回显上游正文/异常。30秒是每次读取超时，不是硬墙钟时限；0.4新增进程内生成任务上限，但仍缺任务级硬截止、费用预算和熔断。

适配 `POST <base_url>/chat/completions`、messages、`response_format={"type":"json_object"}` 与 choices[0].message.content。第三方兼容性并不由OpenAI文档保证；不支持此子集的服务会失败关闭，不静默删掉保护参数重试。本次未配置真实生成服务凭据，**真实LLM端到端、服务商模型质量/费用尚未验证**；已验证的是真实向量模型、真实本地HTTP和适配层确定性故障契约。

## 5. 为什么不直接展示模型的自由回答

模型只返回 `abstain` 与 `[{citation_id, quote}]`。服务端严格拒绝多余字段、伪造ID、非连续原文引句、重复摘录、部分截断响应或工具调用，再由程序生成带引用的答复。这牺牲自由改写，换取每句话可核对；适合先找依据，再由人审核的合规资料场景。

返回的quote有规范化原文字符区间和行号、块ID、版本/级别/安全类和源摘要。区间规则沿用第一阶段：规范化Unicode字符[start,end)，行号1-based。同一quote多次出现时定位首个精确匹配。

**精确引用不是语义蕴含证明。** 相关但缺少加密算法的案例仍会返回相关原文；抽取模式不理解所问事实是否缺失。模型可能选择无关但真实的摘录，恶意原文指令也可能被原样引用。它们不会被执行，但仍需要人工核对；不能据此宣称消除了幻觉或注入。

## 6. 持久化、更新和追踪边界

- 文本、块与向量在SQLite同事务替换；foreign_keys开启，来源删除级联清理。每次真正修改递增全局revision。
- 查询在同一读事务取revision和候选；模型结束后再比对当前revision，变化即拒答且移除旧引用。**最后复核到发送之间仍有窗口**，不承诺线性化撤销或跨实例分布式快照。
- 原始文本和检索结果存本地，未加密；文件目录/备份权限须由运行者保障。
- trace只保存ID、revision、状态/原因、检索/生成模式、候选/引用数量及耗时，保留最近1000条；追踪不是不可篡改审计。
- 每库最多32来源、2000块、8MiB提交文本；输入1MiB、HTTP2MiB/4096帧、问题500字符、top_k≤5、上下文总6000字符。字符限制不等于token预算；BGE另按真实分词器512 token检查，超限拒绝而不静默截断。
- API已有静态角色、持久来源读ACL与单进程额度。没有租户命名空间、段落权限或真实IdP；来源管理员可看全库。标准版本过滤**不是来源授权**。仅单worker回环，后续需IdP/操作审计、硬截止worker和持久任务恢复。CLI/Python本地路径不走HTTP权限和额度，直接库调用者须可信。

## 参考与许可

[官方OpenAI Chat Completions协议](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)及[结构化输出说明](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)用于核对请求格式；JSON模式不等于业务schema正确，必须二次验证。

[FastEmbed官方模型清单](https://qdrant.tech/documentation/embeddings/fastembed/)用于核对中文模型/维度；代码和开发评测不代表模型权重归本项目所有。下载者仍需核对模型自身许可和适用性。
