# 0.4：给共享资料 RAG 加访问权限与有界任务入口

本页的角色、令牌与单进程额度由0.5沿用；普通查询者现在还须取得[来源读授权](SOURCE_AUTHORIZATION.md)。先按本页启动，再审阅并配置来源ACL；仅具查询角色不再默认能看全库。

## 背景、资产与信任边界

0.3的ETag解决旧草稿覆盖，但任何能连接本机端口的进程仍能读库、拿到当前ETag再修改资料；连续模型请求还可能占用线程、内存及外部费用。回环地址和Host校验不认证调用者。本轮直接改造原有入库/查询接口，不另建权限演示仓库。

保护资产包括共享资料正文、来源索引、来源修订、查询结果及模型调用能力。新增边界是：**未认证HTTP → 服务端令牌身份 → 路由角色 → 请求/任务额度 → 原有解析、存储和模型边界**。服务端策略、客户端令牌文件和本地SQLite目录仍须可信。本机同一操作系统用户可读取环境/文件的恶意进程不在防护承诺内。

这是一个**单库、单进程的静态令牌角色系统**。0.4当时reader/analyst共享全部资料；0.5已加入持久来源ACL，默认无授权候选，维护者仍是全库管理员。不具备租户命名空间、段落ACL或用户所有权隔离；版本范围过滤不等于来源授权。没有OAuth、OIDC、IdP登录、密码认证或令牌在线撤销，来源读权限可在线事务更新。

## 权限表

| 能力 | reader | analyst | maintainer |
| --- | --- | --- | --- |
| POST /v1/search；POST /v1/ask extractive | 仅获准来源 | 仅获准来源 | 全库允许 |
| POST /v1/ask generation=llm | 403，检索/服务初始化前拒绝 | 允许尝试 | 允许尝试 |
| POST /v1/parse | 403 | 403 | 允许 |
| GET /v1/library；POST /v1/library/source | 403 | 403 | 允许 |
| POST /v1/library/ingest、/remove | 403 | 403 | 允许，仍须ETag条件 |
| POST /v1/library/access、/access/update | 403 | 403 | 允许，修改需access ETag |
| GET /v1/traces/{id}；GET /openapi.json | 403 | 403 | 允许 |
| GET /health | 公开的存活状态，不检查数据库/模型就绪 | 同左 | 同左 |

只接受一个 `Authorization: Bearer <token>`，不从URL、JSON、Cookie或X-Forwarded-For读取身份，不接受客户端指定role。认证方案名称大小写不敏感，凭据本身大小写敏感。重复头、Basic、逗号合并令牌、超长/短令牌统一401且不回显。正常生成许可还须原有服务端 `RAG_ENABLE_EXTERNAL=1`、可信模型配置和客户端 `generation=llm`；角色不能代替外传许可。

路由采用精确方法/路径白名单，新增路由默认403；尾斜杠不会自动绕到重定向。`/docs`、`/redoc`页面关闭，防止匿名文档入口；维护者可通过带认证的HTTP客户端读取OpenAPI JSON，其中记录Bearer要求。此处使用本地不透明令牌，不宣称遵循完整OAuth协议。

## 安装与启动：密钥留在本机

在仓库根目录、已激活虚拟环境后运行。初始化只创建**全新**目录，已有目录报错，不替换旧令牌；不会把令牌打印到终端。

```powershell
python -m compliance_rag.access output/access
$env:RAG_ACCESS_JSON = Get-Content -Raw -Encoding utf8 output/access/access.json
# 选择新的演示库；保留已有真实资料库和备份。
$env:RAG_DB_PATH = 'output/demo-v04.sqlite3'
python -m uvicorn compliance_rag.api:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

目录里有三个随机令牌文件 `reader.token`、`analyst.token`、`maintainer.token`，以及只保存SHA-256摘要的 `access.json`。令牌由 `secrets.token_urlsafe(32)`生成，256位随机输入；SHA-256摘要适合此类高熵随机凭据，**不用于存用户密码**。认证用固定长度摘要的compare_digest；不声称整个HTTP链路时序恒定。

POSIX新目录/文件分别请求0700/0600；Windows仍继承本地NTFS ACL，代码不自动改变用户权限。将目录放在自己可控的位置，不共享、提交或上传令牌。output、*.token、access.json均被Git忽略，忽略规则不替代文件权限或秘密扫描。初始化中途IO失败可能留下部分新文件，修复环境后换新目录初始化，勿把不完整输出当成已启用。

在**另一终端**加载维护令牌运行现有完整演示：

```powershell
$env:RAG_API_TOKEN = (Get-Content -Raw -Encoding utf8 output/access/maintainer.token).Trim()
python -X utf8 examples/rag_api_client.py
# 或只演示原有解析接口：
python -X utf8 examples/api_client.py
```

示例仅发送原创合成资料。已有演示来源就停止；不要为了重复运行覆盖用户更正。客户端变量 `RAG_API_TOKEN` 与生成服务的 `RAG_LLM_API_KEY` **用途不同**：前者认证本地API，后者仅供服务端连接模型。

只查询的客户端应加载reader令牌，用HTTP客户端发送JSON：

```python
import os
import httpx

payload = {"question": "进入记录保留多久？", "scope": {
    "standard_code": "TRAINING-POLICY", "edition": "v2", "security_level": "第三级"}}
with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False,
                  headers={"Authorization": "Bearer " + os.environ["RAG_API_TOKEN"]}, timeout=40) as client:
    result = client.post("/v1/ask", json=payload)
    print(result.status_code)
    if result.status_code == 200:
        print(result.json()["answer"])
```

这份Python例子使用启动它之前本地加载的令牌，不在源码中保存真实凭据。常规示例客户端不自动重试，也不打印认证头。启动命令关闭访问日志以减少URL留存；不接受URL令牌不意味着可以把秘密放URL。代理/APM/调试日志的保密需运行者单独保障。HTTP明文只用于本地回环，不要把Bearer令牌经公网HTTP传输；本轮不提供公网部署。

## 策略格式与额度

`RAG_ACCESS_JSON`是服务端环境变量中的JSON字符串，启动时校验并复制为不可变身份集合；不自动读取.env，不从请求读取文件。最多16个身份，id和摘要都必须唯一，多余字段/未知角色/重复JSON键/非法限额/超过16KiB均报静态配置错误。未设置变量时，所有非健康接口503锁定；设为空或损坏时启动失败，**没有匿名降级模式、内置部署凭据或默认密码**。

可编辑私有access.json再加载环境并重启。下面只列字段，摘要占位须换为初始化生成的真实64位小写hex，不可直接运行：

```json
{
  "principals": [
    {"id": "local-reviewer", "role": "reader", "token_sha256": "REPLACE_WITH_64_LOWERCASE_HEX_DIGEST"}
  ],
  "limits": {
    "requests_per_minute": 120,
    "active_total": 4,
    "active_per_principal": 1,
    "active_llm": 1
  }
}
```

- **请求频率**：按服务端身份ID记录最近60秒的获授权请求，使用monotonic时钟和互斥锁。边界恰好60秒的旧项过期。容量忙、无效JSON、ETag冲突或后续失败也计入；未认证/角色不允许的请求不创建计数项。超过上限返回429/request_rate_limited与到下一个名额的向上取整Retry-After，不因拒绝而延长窗口。
- **任务容量**：认证和路由授权后、读取正文前原子取得全局及身份名额。满额立即429/task_capacity_exhausted，无应用任务等待队列；正文慢读也占名额，直到HTTP结束/取消。
- **生成容量**：进入同步ask工作区、初始化服务/检索前，再限制llm模式任务数。reader直接403；满额429/generation_capacity_exhausted。这个额度保守覆盖整个llm请求，即使最终无证据或模型未配置也占用，不等于实际网络调用次数。
- **占用生命周期**：名额同时追踪HTTP和实际同步worker。二者均结束才归还；HTTP被取消而模型/嵌入仍运行时，仍保留名额；取消发生在worker启动前则禁止它后来执行。worker异常也在finally释放。这里不通过杀线程“取消”SQLite提交或模型调用。
- **Retry-After=1**：容量繁忙时只是重试提示，不保证一秒后完成。调用者需自行决定有界等待；写请求不能因此自动读取新ETag重试覆盖。取消/丢失响应也不能证明没有提交，须按0.3指南查看并人工核对来源。

默认值是保护策略，不是性能测量。可配置范围：请求1..6000/分钟，全局任务1..32，单身份1..8，llm任务1..8；实际同时受所有上限约束。计数仅为最多16个配置身份保留，不按攻击者任意IP/id建立无限字典；窗口队列有上限。**这不是token/人民币预算**，不计算服务商费用，不持久保存额度。进程重启重置频率，多个worker/实例各有独立额度，所以必须单worker运行；本轮没有分布式限流或持久账本。

## 最难的问题：HTTP取消不等于工作结束

若在ASGI中 `finally: semaphore.release()`，取消HTTP会归还名额，但已经开始的同步生成线程仍可能继续运行；反复取消可绕过真实并发上限。本轮在同步路由中显式使用 `with work(request, external=...)`，租约的HTTP占用和worker占用在同一锁下更新。只有二者都结束才减少计数，外部任务计数由worker自行归还。

反过来，如果只在同步路由内抢锁，未认证/满额请求已经先读入正文、等待线程池，入口内存仍未受保护。因此采用“正文前取得名额 + 真正工作时保留占用”的组合。中间件顺序是Host检查 → 认证/角色/准入 → 原有字节/帧/JSON门禁 → 路由与同步worker；health不缓冲GET正文。

这仍然**没有硬任务截止时间**。获授权的慢客户端或持续流式上游可长时间占用有限槽位，导致其他请求拒绝；限制的是并发数量，无法保证可用性或总墙钟时长。下一步应采用有硬配额/截止时间的独立worker及可靠任务状态，而不是把Python线程取消说成硬中止。

## 复现与证据

```bash
python -X utf8 -m compliance_rag.access_demo
python -X utf8 -m compliance_rag.live_smoke
python -X utf8 -m pytest -q
```

`access_demo`实际调用FastAPI和临时SQLite，但使用**进程内ASGI客户端、受控本地selector替身**，没有真实LLM。令牌运行时随机生成、不写入报告。输出与[固定合成证据](../examples/evaluation/access-admission.json)逐字段一致：

| 情境 | 实际断言 |
| --- | --- |
| 无认证读库、reader删除、reader抽取、reader llm | 401/403/200/403；拒绝写入前后revision不变 |
| 一个analyst任务持有槽位，另7个同身份请求 | 1个200、7个429，本地selector只调用1次 |
| llm全局额度满时另一身份请求 | maintainer llm=429，reader抽取=200 |
| reader连续3次查询，频率上限2 | 200/200/429，完成后任务槽位归零 |

`live_smoke`另外启动真实临时回环Uvicorn，用实际HTTP核验401/403/200/403及既有入库、问答、ETag更新、trace、删除、拒答链。不是公网部署或真实模型调用。

`tests/test_access.py`还覆盖实际ASGI任务取消+仍在运行的AnyIO线程、先取消后worker启动、异常/超限正文释放、精确时间边界、32次并发5次频率准入、角色矩阵、重复头/JSON、未知路由、配置损坏不降级、令牌初始化不覆盖等。没有伪造旧版利用测量：0.3的匿名风险来自旧代码分析，当前证据验证的是0.4合同。

## 误拒绝、漏防与升级代价

- 合法的突发、多终端共用一个身份也会被429；为不同客户端配置独立身份可分别记账，但不能突破全局容量。慢客户端与健康轮询的传输层DoS不在完整保护范围。
- 被盗或主动共享的令牌仍可使用角色及来源ACL允许的能力。0.5增加整来源读ACL，仍无段落/租户权限、MFA、令牌有效期、即时令牌撤销、不可篡改审计或管理员防篡改；维护者可访问全库。
- 策略启动时固定。轮换到全新令牌目录并更新服务/客户端后，应停止全部旧进程；修改环境不会让运行中的请求立刻撤销，已开始任务可能完成。历史凭据不要提交Git，旧输出也不要自动公开。
- CLI和Python库是可信本地维护路径，绕过HTTP身份和额度；本机文件写权限仍可直接改SQLite。不能靠API角色约束恶意本机管理员。
- 历史0.4不改schema2，但所有业务HTTP新增令牌要求，Swagger页关闭；当前0.5还会升级到schema3、默认不给旧资料普通读权限，见[升级说明](SOURCE_AUTHORIZATION.md)。必须停止全部旧服务并私有备份，不能与已初始化的旧匿名/无ACL服务混跑。
- 这些控制没有提高20问开发集的语义拒答率；“相关但缺少所问事实”仍可能给原文摘录，真实生成质量和独立验证仍待完成。

## 参考

Bearer持有者即可使用对应权限，应保护存储和传输，参见[RFC 6750](https://www.rfc-editor.org/rfc/rfc6750.html)。固定摘要比较采用[Python hmac.compare_digest](https://docs.python.org/3/library/hmac.html#hmac.compare_digest)。本项目没有实现RFC对应的授权服务器或全部协议，只采用Authorization头的受限本地合同。
