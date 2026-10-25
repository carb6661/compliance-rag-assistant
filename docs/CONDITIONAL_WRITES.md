# 0.3：避免知识维护中的丢失更新与误删除

## 背景：事务原子不等于没有丢失更新

一个分析员在两个终端打开同一份RAG资料，两次都读到旧内容。终端A先提交更正，终端B稍后提交旧草稿。0.2的SQLite事务能保证“来源、块、向量一起替换”，却没有比较B是否基于当前来源，仍可能把A的更正覆盖。

这不是向量模型问题。检索再准确，索引维护把新事实覆盖成旧草稿，回答仍会退回错误资料。0.3在**同一RAG项目**加入来源级条件写，解决知识维护这个实际环节，而非另建一个CAS小工具仓库。

## 设计

- 每个库持久保存随机 `instance_id`；每次来源真正改变获得新的 `source_generation`（取本次新全局修订号）。
- `source_etag`绑定库身份、精确来源标签、来源generation、提交文本摘要和解析配置摘要，以带引号的64位小写hex形式返回。
- HTTP创建必须 `If-None-Match: *`；更新/删除必须带读取时取得的 `If-Match: "<64hex>"`。
- 先做廉价预检查，再解析/本地向量计算；进入 `BEGIN IMMEDIATE` **后再次读取来源并比较条件**，通过才替换/删除及提交。不能只在模型计算前查一次。
- 条件失败不改来源、块、向量或全局修订号，不返回最新token。调用者必须重新读取、核对差异，再决定是否提交，不能自动拿新token覆盖。

ETag是公开的版本校验值，**不是认证令牌或用户授权**。任何能读写本机接口的人仍可取得当前值；本版本仍是私有回环单用户应用。

提交成功但响应丢失时，用旧标签重试也会412；这不证明上一次没有提交。需要读取核对现状，不能把本合同冒充持久幂等回执或exactly-once。来源读取提供规范化正文及配置摘要，不恢复配置原件；调用者仍须保留并审阅自己的解析配置。

## 为什么不能只用正文哈希

内容A改为B又改回A，或删除A再创建相同文本，正文哈希都可能再次相同。旧编辑者却没有看过中间变化，旧token不能因此重新有效。新的来源generation和库身份会使标签改变；测试分别覆盖内容ABA与删除/重建ABA。

另一个细节：来源标签不应因**其他来源**的更改而失效，因此它不直接使用当前全局revision。来源读取响应也不包含随其他来源变化的全局revision，避免出现“相同强标签、不同来源表示”的矛盾。全局revision仍用于RAG生成后复核，来源ETag用于维护前置条件，两者职责不同。

## API合同（有意限制的子集）

逻辑来源由请求体 `source_name` 选择。沿用现有POST操作接口，不声称实现通用HTTP缓存或完整条件字段语法。只支持一个本应用生成的强标签；弱标签、列表、`If-Match: *`、同时提供两类条件、重复条件头均400拒绝，不猜测含义。

| 操作/结果 | 合同 |
| --- | --- |
| POST /v1/library/source | JSON `source_name`；返回规范化文本、摘要、source_generation及source_etag；响应ETag与body一致；不存在404 |
| POST /v1/library/ingest + If-None-Match:* | 仅创建不存在来源；已存在即412，包括内容完全相同 |
| POST /v1/library/ingest + If-Match | 条件匹配才替换；内容/配置均未变则unchanged，不增长generation |
| POST /v1/library/remove + If-Match | 只删除读过且仍未变的来源；删除后再次用旧token返回412，而不是伪装成新的删除成功 |
| 缺少条件 | 428 precondition_required；不作隐式覆盖 |
| 陈旧或不存在来源的匹配条件 | 412 precondition_failed；不回传当前token |
| 语法不支持/歧义 | 400 invalid_precondition |

来源、清单、变更响应及条件错误显式 `Cache-Control: no-store`；错误不回显正文/草稿/底层路径。读取用POST体传来源名，避免将业务标签放入常规访问日志的URL查询字符串。请求体日志/反向代理仍需操作者独立配置。

引用和检索hit新增 `source_etag`，绑定查询读事务实际看到的来源状态。它不是下载权限，也不证明来源真实或标准合法。

## Python与CLI

```python
from compliance_rag.conditions import WriteCondition, WriteRejected

# 新建：store.ingest(text, name, config, condition=WriteCondition.create())
snapshot = store.source(name)  # 本地私有数据，不要写进公开日志
# 用户审阅 snapshot['normalized_text'] 后提交自己的修订
try:
    result = store.ingest(edited_text, name, config,
                         condition=WriteCondition.match(snapshot['source_etag']))
except WriteRejected as error:
    if error.status == 412:
        pass  # 停止；重新读取、比较和人工合并，不自动重试覆盖
```

新建样本：

```bash
python -X utf8 -m compliance_rag.rag_cli ingest examples/knowledge/v2.md --source-name knowledge/v2.md --config examples/knowledge/config-v2.json --create-only
python -X utf8 -m compliance_rag.rag_cli source knowledge/v2.md
```

审阅后用返回的标签更新：`ingest ... --if-match <etag>`，删除：`remove knowledge/v2.md --if-match <etag>`。CLI也接受不含引号的64hex，避免Shell引号传递差异；HTTP仍要求带引号标签。没有 `--force`。CLI冲突退出3、输入/配置问题2、来源不存在4；原问答拒答仍退出1。

`examples/rag_api_client.py`现在只创建样本，发现已有来源就停止，**不会先取新token再偷偷覆盖**。重复演示请在启动服务前选择新的 `RAG_DB_PATH`；旧数据保留，不能为了演示一键清库。

## 0.2升级与数据保护

1. 停止所有旧版写进程，在没有写者时按自己的备份规范保存私有SQLite；不要把库上传GitHub。
2. 新版首次打开schema1库时，在单个IMMEDIATE事务中增加generation、生成库身份并切到schema2；旧来源以迁移时全局revision作为generation锚点，不伪造历史更新顺序。
3. 不重算正文、块ID、metadata或向量。独立schema1夹具迁移后逐行比较12个原块；故障注入验证列与settings一起回滚；并发初始化验证只迁移一次。
4. 旧客户端必须添加条件头/CLI参数；原 `/v1/parse` 与Document格式保持兼容，解析器版本仍是0.1.0，应用版本是0.3.0。

这是有数据写入的schema升级。**不支持旧、新写者混跑或自动降级**；不访问或迁移用户工作区中的真实私有库来做演示，所有验证均使用临时合成库。

## 实测证据与局限

```bash
python -X utf8 -m compliance_rag.conditional_demo
python -X utf8 -m compliance_rag.live_smoke
python -X utf8 -m pytest -q
```

[固定合成证据](../examples/evaluation/conditional-writes.json)中：缺条件428，A提交200/B旧草稿412，A的181天更正保留；两种ABA旧标签都412；8个独立对象/连接并发写只有1次提交、7次冲突、revision只增加1。测试另实际启动4个CLI子进程，共用同一库与同一个旧token，退出码为1个0、3个3，只有一次写入生效。

真实回环HTTP新增状态序列 `[428,200,412,412]`，并读取核验181天后才用新标签删除；原检索/trace/删除后拒答流程继续通过。没有运行旧版漏洞HTTP服务；0.2失更新风险基于其已公开的无条件写代码路径分析，不把模拟场景标成真实旧版对照。

剩余限制：同一SQLite文件、所有写者遵循本版本合同、私有可信本地文件系统；不是多主/多地域分布式一致性、编辑锁、自动合并、历史版本仓库、审批或完整审计。恢复旧备份/直接改SQL/恶意本机文件替换可能破坏generation假设。生成后的最终发送窗口、无真实LLM联调和相关无事实的拒答缺陷也没有由此消失。

## 设计参考

[RFC9110的If-Match语义](https://www.rfc-editor.org/rfc/rfc9110.html#name-if-match)和[RFC6585的428](https://www.rfc-editor.org/rfc/rfc6585.html#section-3)用于区分陈旧条件与缺少条件；本接口是上文明确限制的应用合同，不是完整HTTP协议实现。[SQLite事务说明](https://www.sqlite.org/lang_transaction.html)用于核对单写者/IMMEDIATE检查边界；原子事务本身并不自动替应用比较旧草稿。
