# 0.5：来源级授权进入真实RAG证据链

## 背景：有查询角色，不代表能看整库

0.4验证调用者身份、区分查询/生成/维护并限制任务容量，但reader/analyst仍共享所有已导入资料。两个分析员维护同一标准、版本、级别的不同来源时，业务scope完全相同，版本过滤无法防止跨资料读取；top_k后的过滤还可能让未授权块参与评分、模型上下文或统计。

0.5在原项目加入**SQLite持久来源ACL**，将允许哪些身份读取哪份Markdown与内容修订绑定。资产包括正文、向量、评分统计、引用和对外生成上下文；边界为“已认证角色 → 当前来源读权限 → 评分/上下文 → 发布检查”。所有公开文件均为原创合成，不是国家标准、客户资料或真实组织权限清单。

本轮实现的是**单库、按完整来源文件授权**。维护者是可信管理员，可以读取全部来源、改正文和授权；普通查询者需要显式获准。没有租户命名空间、来源所有权、段落级ACL、真实IdP或不可篡改操作者审计。它与早期内存Authorization Lab不同，直接接入现有SQLite、真实BGE和引用链路，不把那个实验通过当作本项目的权限证明。

## 数据与设计

- schema3在sources增加 `access_generation`，新表 `source_grants(source, principal_id, credential_sha256)` 使用 `(source, principal_id)`主键和来源外键级联。每来源最多16个读者，实际受服务端最多16个配置身份约束。
- 授权绑定**身份ID与当前令牌SHA-256摘要**。客户端不能提交摘要、role、identity或allowed_sources。令牌轮换/复用相同ID不继承旧授权；维护者须用新配置重新授权。摘要不是明文令牌，仍保存在私有库，不经ACL接口返回。
- ReadIdentity仅由认证中间件的服务端Principal构造，传入实际search/ask。maintainer有显式全库权限。Python库/CLI默认identity=None属于可信本地维护路径，不走HTTP认证、ACL和额度；不能把库直接暴露给不可信代码。
- 每次读在一个SQLite读事务中取得全局revision、来源ETag及**同时满足标准/版本/级别/安全类和读ACL的候选**。SQL EXISTS在评分前执行，未授权内容/向量不会进入候选、BM25统计或模型材料。没有授权候选时跳过查询嵌入，返回零候选/无证据，不回退到其他来源或版本。
- 评分后、模型调用前和答案发布前验证revision及当前读权限。中途变化时，search返回409/knowledge_or_access_changed且无片段；ask返回HTTP200、status=refused/reason=knowledge_changed，答案、引用、retrieval.hits和candidate_count全部清空。无授权与不存在资料均表现为no_evidence，不承诺时间侧信道等价。

```text
服务端令牌身份 → 路由角色/任务额度 → scope + ACL的SQL过滤
             → 授权候选 → BM25/BGE/RRF → 修订/权限复核
             → 本地抽取或显式生成 → 引用校验 → 再复核 → 证据或拒答
维护者改授权 → 同事务更新ACL/generation/全局revision
维护者改正文/配置 → 同事务替换来源并清空读ACL → 重新审阅授权
```

只查当前ACL而不查修订会漏掉“撤权后又授权”的ABA；因此所有真实ACL变更都会增长全局revision及本来源access_generation。全局revision较保守：即使变更的是无关来源，也可能让进行中的查询拒答。当前规模较小，优先可解释的失败关闭，没有引入分布式权限缓存或复杂增量版本。

## 两类ETag必须分清

| 标签 | 读哪里 | 用途 | 变化条件 |
| --- | --- | --- | --- |
| source_etag | POST /v1/library/source | If-Match修改/删除正文 | 内容、配置、来源generation/库身份；ACL变化不改它 |
| access_etag | POST /v1/library/access | If-Match替换完整读者集合 | 来源ETag、access_generation、身份/凭据绑定集合 |

ACL响应的HTTP ETag是access_etag。GET式读取使用POST JSON，避免把业务来源名放URL。授权标签绑定读到的正文状态；内容已变、删后重建、旧ACL标签、两类标签混用、另一个来源标签都412拒绝。缺条件428；弱、列表、重复或create条件按原受限合同400拒绝，不自动取最新标签重试。

ACL读取JSON包含source_name、source_etag、access_etag、access_generation、readers（按ID排序），不含全局revision、正文或令牌摘要。无关来源变更不改变这份表示及其ETag。完全相同的**身份和凭据绑定**替换返回unchanged且不增修订；令牌换了即使readers ID列表相同，也是真实授权变更。令牌摘要参与access_etag计算但不直接返回。

**真正修改正文或配置会清空全部普通读者授权**，返回 `reader_grants_cleared` 数量。安全默认是审批的那份内容才能继续被读；维护者审阅后重新授权。相同原文/配置的no-op保持授权。删除来源级联清理；同标签名重建也不继承权限。这个行为会使合法更新后的查询暂时无证据，不是自动恢复或审批流系统。

## 运行与操作

先按[访问令牌指南](ACCESS_CONTROL.md)初始化本机令牌，配置私有RAG_ACCESS_JSON，单worker回环运行。初始化默认身份为reader、analyst、maintainer，不需给模型服务密钥即可验证本轮。以下客户端固定连接127.0.0.1:8000，只从本地环境读取维护令牌，无自动重试。

```powershell
# 独立客户端终端；令牌值不打印、不复制到聊天或源码
$env:RAG_API_TOKEN = (Get-Content -Raw -Encoding utf8 output/access/maintainer.token).Trim()
# 假定已通过原演示导入knowledge/v2.md；先读私有原文并审阅
python -m compliance_rag.rag_cli source knowledge/v2.md
# 获取授权快照，核对source_etag对应刚审阅的正文
python -X utf8 examples/manage_source_access.py knowledge/v2.md
```

把授权快照的access_etag填入下一条占位（可使用去引号的64位hex，勿照抄占位）：

```powershell
# 完整替换为这两个已配置查询身份，保留其他人需显式列出
python -X utf8 examples/manage_source_access.py knowledge/v2.md --set-readers reader analyst --if-match REVIEWED_ACCESS_ETAG
# 撤销所有普通读者（仍需另一次刚读到并审阅的授权标签）
python -X utf8 examples/manage_source_access.py knowledge/v2.md --set-readers --if-match REVIEWED_ACCESS_ETAG
```

`--set-readers`无ID表示空集合；列表不是追加。CLI读取原文时须指向与服务端相同的私有数据库/模型配置；默认API和CLI均为output/knowledge.sqlite3，若服务器自定义了RAG_DB_PATH，CLI需显式 `--db`；向量库还需原有 `--model-path`。不要把不同数据库的标签混用。

之后切换客户端为reader令牌再按RAG指南调用search/ask；未授权前HTTP200/no_evidence，授权后可获得对应来源引用。analyst还可尝试llm，但仍须服务端外传许可/真实服务配置；本轮不需要也没有调用付费生成服务。maintainer始终可见全库，不能用维护令牌演示普通用户隔离。

### API路由

| 方法/路由 | 权限 | 输入和结果 |
| --- | --- | --- |
| POST /v1/library/access | maintainer | `{source_name}` → 授权快照及access ETag；来源不存在404 |
| POST /v1/library/access/update | maintainer | `{source_name,readers:[id...]}` + If-Match授权标签 → 完整替换后的快照/status |
| POST /v1/search | 原有查询角色 | 自动使用服务端身份；只返回获准候选；快照变化409且无内容 |
| POST /v1/ask | reader抽取；analyst/maintainer可生成 | 自动带来源ACL；变化则业务拒答并清空证据 |

readers必须是不重复、当前服务端已配置的reader/analyst身份，最多16项；未知、maintainer、歧义输入、额外字段均422。查询角色不能读ACL、自己授权、访问全量来源索引/原文或全局trace，先由中间件403，不先读取正文。API错误与成功结果沿用no-store及静态错误。

## 升级：schema2 → schema3

1. 停止全部旧服务/写者，私有备份数据库。尤其不能让已初始化的0.4服务和0.5混跑：旧查询路径不执行新ACL，旧代码缓存的store也可能继续工作。
2. 0.5首次打开schema2库，在IMMEDIATE事务中添加ACL列/表并改schema标记3。ACL迁移失败会一起回滚新列、新表、schema标记；不改旧原文、块、向量、instance_id、source generation、source ETag或全局revision。schema1先执行原迁移再进入3。
3. 旧资料**不自动给reader/analyst整库授权**；维护者仍能访问并逐来源审阅、添加授权。默认拒绝是预期升级行为。
4. 使用原服务端访问策略与令牌；原令牌不变的授权绑定在后续重启继续有效。轮换令牌、复用ID或正文更新后，需要重新审阅授权。所有实例须使用一致且受信的身份配置；本轮没有跨实例令牌撤销协议。

不要在生产私有库上试验失败迁移。本项目测试只使用临时、独立旧schema夹具；没有擅自升级工作区的真实资料库。

## 实验与实际证据

```bash
python -X utf8 -m compliance_rag.source_access_demo
python -X utf8 -m compliance_rag.live_smoke
python -X utf8 -m pytest -q
```

source_access_demo使用两份原创虚构资料，**标准/版本/级别/类别/标题完全相同**，甲组180天、乙组999天且有重复关键词。SQL来源授权是区分它们的依据。它实际调用FastAPI、SQLite及引用渲染，selector为受控本地测试替身、传输为进程内ASGI，未调用真实LLM。输出与[固定证据](../examples/evaluation/source-access.json)逐字段相同：

- 默认零候选；Alice只看到甲组、Bob只看到乙组，模型替身只收到甲组。
- selector运行时经维护接口撤权，最终reason=knowledge_changed，答案、引用、检索片段及候选数全部清空；旧授权标签再提交412。
- 正文更新180→181清空1项授权；重审前no_evidence、旧授权标签412、新标签重新授权后返回181天。
- 重开SQLite后授权仍存在。无随机令牌、摘要、私有路径或真实资料进入固定报告。

live_smoke另起真实Uvicorn回环HTTP，验证未授权无证据→经维护者授权有证据→内容替换后再无证据，保留0.3条件维护和0.4角色检查。不是公网或真实生成服务实测。

可选真实BGE运行（先按RAG指南准备本地模型）：

```bash
python -X utf8 -m compliance_rag.semantic_smoke --model-path LOCAL_PREPARED_MODEL_DIR
```

本轮已在Windows/Python3.12.8以实际512维中文ONNX运行，禁止出站socket/DNS：新两块的BM25/Dense/Hybrid各1个授权候选且仅引用甲组；原20问开发集结果不变、不可回答仍只拒3/4，超512 token仍拒绝。CI不下载权重；可选集成与协议替身分开报告。

专项测试还验证评分输入、BM25统计等同于仅授权语料、无授权不调用查询嵌入、不同凭据同ID不继承、配置变化清空授权、ACL ABA、8连接竞争仅1提交/7冲突、事务故障完整回滚ACL及修订、向量计算期间撤权、生成前撤权跳过selector、生成中撤权、独立schema2的12块保留/迁移回滚/并发升级等。不运行旧0.4有漏洞的联网服务，不伪造前后攻击成功率。

## 不能承诺什么

- **检查不是撤回**：SQL读取、发送模型请求前检查、模型返回后检查到实际HTTP发送之间仍有窗口；不是线性化/即时全局撤销。材料已送到服务商无法撤回，已发给客户端的字节也无法撤回。生产数据外传仍需独立许可。
- **管理员可信**：维护者可授权自己控制的身份；本机数据库/备份/代码写权限可绕过控制。令牌仍是静态服务端配置，没有IdP、令牌在线撤销、有效期、MFA或操作审计；来源ACL可以在线更新，不要混淆两类撤销。
- **授权不认证资料**：被授权的恶意/不相关原文仍可能误导选摘。来源信任仍unverified，ACL不证明资料真实性、正确性、版权许可或答案语义支持。
- **粒度与可用性取舍**：完整来源文件为最小单位，混合敏感度要先拆文档；更新/配置变更清空读者会短暂拒绝合法查询；无关来源修改也可能触发保守拒答。
- **运行边界未变**：额度仍单进程、重启清零，无硬任务截止、持久任务状态、费用预算或多租户隔离。本轮没有提高语义指标或实现完整测评判定。

参考：[OWASP授权设计](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)的默认拒绝/逐请求检查原则，以及[SQLite隔离](https://www.sqlite.org/isolation.html)的事务读取语义。具体检查点、来源失效及管理员边界为本项目设计，不能由这些参考资料直接推出生产安全保证。
