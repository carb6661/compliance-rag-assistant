# 0.6：外部证据选摘的墙钟预算与子进程监督

## 背景与范围

0.4限制并发，0.5限制来源权限，但一个合法生成请求仍可能占槽很久。HTTPX的read timeout限制等待下一段数据的时间，不是整次调用的总时间；服务商每隔一小段时间发一个字节，仍可能一直没有完整响应。取消HTTP协程也不会终止已经开始的同步线程。

0.6把**标准服务端工厂创建的外部证据选摘**放进一个固定Python子进程，由父进程监督墙钟预算、输入/输出和回收。默认45秒，超时只终止本次Popen创建的子进程，不查找进程名、不执行用户命令、不自动重试。原SQLite检索、来源授权、原文引用校验和最终修订检查留在父进程。

这是**生成阶段预算**，不是整个请求的硬时限，也不是OS安全沙箱。正文接收、BGE初始化/查询、SQLite等待、请求排队前的网络行为不在该预算内。操作系统进程创建无法在所有平台上被中断，OS调度和回收也可能增加耗时；不能把“配置45秒”写成“HTTP一定45秒内结束”的SLA。

## 数据流与信任边界

```text
已认证/获准RAG请求 → SQL候选 → 生成前权限/修订复核
  → 获取worker容量 → 固定Python -I子进程
  → stdin：固定服务配置 + 问题 + citation_id/证据文本
  → 兼容HTTP服务（原有显式许可、固定URL、单次调用）
  → stdout：受限JSON选摘/静态错误
  → 父进程协议/schema验证 → 确认子进程退出/管道结束
  → 原文引用及最终权限/修订复核 → 证据或拒答
```

父进程和worker代码、Python运行时、包目录及管理员选择的服务商仍须可信。worker没有数据库对象、完整元数据、来源路径、API访问令牌或本地嵌入模型配置；只得到至多6000字符证据及500字符问题。它仍以当前OS用户运行，**不限制文件权限、内存/CPU、系统调用或恶意本地管理员**。提示词/响应不会变成Python、shell或工具调用。

## 服务端配置

仍按[RAG指南](RAG_GUIDE.md)和[来源授权](SOURCE_AUTHORIZATION.md)运行。模型服务配置及密钥只放服务端本地环境，不在聊天、命令参数或源码中提供真实密钥：

```powershell
$env:RAG_ENABLE_EXTERNAL = '1'
$env:RAG_LLM_BASE_URL = 'https://你的已授权服务域名/v1'
$env:RAG_LLM_MODEL = '服务商实际模型名'
$env:RAG_LLM_API_KEY = Read-Host '仅在本机输入模型密钥' -MaskInput
$env:RAG_LLM_DEADLINE_SECONDS = '45'
$env:RAG_LLM_WORKERS = '1'
# 访问策略/来源ACL另按原指南配置；仍单worker回环部署
python -m uvicorn compliance_rag.api:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

`.env`不自动加载。API首次初始化RagService时创建生成器，配置随后固定，变更须重启服务。未启用外传/缺少生成服务配置仍返回原model_not_configured，不启动worker；reader依旧不能请求llm模式，未授权来源不会进入worker。

| 配置/预算 | 默认与范围 | 含义 |
| --- | --- | --- |
| RAG_LLM_DEADLINE_SECONDS | 45，0.25..120有限秒数 | 从本次worker保留容量后开始，覆盖启动、管道、HTTP及响应验证；启动API本身可能不可中断 |
| RAG_LLM_WORKERS | 1，整数1..8 | 每个IsolatedGenerator实例允许的在用/未确认回收名额总数，无等待队列 |
| 回收宽限 | 默认1秒 | 尝试kill、wait及等待两条管道任务结束；与生成预算分开，不是延长成功接收窗口 |
| stdin/stdout包 | 各最多65536字节 | UTF-8严格JSON，无pickle；父进程读取stdout也最多多观察1字节以发现超限 |
| 生成材料 | 问题≤500字符、证据1..5项且总≤6000字符 | 只传ID/正文，不传来源元数据；单证据≤2000字符 |

这些与API的active_total、active_per_principal、active_llm并行生效。API额度满时仍是429；已获API名额但worker池繁忙，则按原生成业务合同返回HTTP200/status=refused/reason=generation_worker_busy。所有调用者共享同一服务端生成器；多个应用/进程各自有容量，**不是分布式或跨实例上限**。

只要配置了外部生成，标准generator_from_env默认使用IsolatedGenerator，没有客户端关闭隔离或修改命令、预算、worker数的字段。直接使用CompatibleGenerator是可信库调用/协议测试层；自定义注入的generator不自动受这个监督器约束。

0.6不改变schema3、来源ACL或两类ETag，数据库不需新增迁移。正常停旧服务，保留私有数据库/访问策略后启动新版即可；原0.5来源更新重审规则不变。worker依赖同一Python环境及可读取的包文件，缺依赖/无法创建进程会失败关闭，不回退为未监督调用。

## 真正困难的部分

### 超时必须能打断等待，而非只停止等结果

不能用线程Future.result(timeout)后就归还槽位：原调用线程可能仍持有HTTP连接。这里模型HTTP在独立进程，父进程只对自己持有的Popen对象执行kill/wait；成功输出也要等到进程退出并确认管道任务结束，迟到结果不会被接纳。

仅把Popen.communicate(input=..., timeout=...)放在请求线程也不够稳妥：管道写入可能阻塞，Windows的写路径尤其需要仔细处理。本实现用独立、数量有界的写/读辅助线程处理管道，主监督线程只等Event和单调时钟预算；测试启动真实的“不读取stdin、只等待”的自有子进程，仍可按预算终止并回收。辅助线程每worker最多两条，随该worker占用一同记账。

### 无法确认回收时不能假装有空位

每个worker记录同时持有进程、writer_done与reader_done。只有**进程已退出且两条管道任务完成**才释放worker容量并关闭句柄。kill/wait/poll失败或I/O未结束时，记录转quarantined，保留容量，返回generation_worker_cleanup_failed。下一次容量检查仅在观察到真实退出/管道结束后回收该记录，不能通过再次请求或超时自动“清零”。

隔离名额不等于持久任务状态。Python调试接口generator.state()只给occupied/quarantined计数，不暴露PID/资料；它没有公开HTTP路由，也不是持久账本。异常进程回收的故障注入使用明确的FakeProcess；没有伪装成真实OS进程无法kill的实测。

### HTTP取消与生成预算是两条边界

沿用0.4租约：HTTP被取消后，已经开始的同步worker仍持有API/生成名额，直到子进程按预算完成或回收流程返回。新增ASGI取消测试使用真实自有子进程，验证取消后名额仍为1、进程确已结束后才为0。若回收未确认，API请求租约可能结束，但独立worker池仍保留隔离容量，后续不能再创建worker。

### 输入与异常不能变成秘密泄漏渠道

固定命令为当前Python的`-I`模式和项目内provider_worker.py，shell=False。子进程不继承RAG访问令牌、DB/模型路径、代理、PYTHONPATH或生成密钥；只保留必要OS临时目录/系统环境，生成密钥经私有stdin传入。密钥仍存在两进程内存，不是加密或密钥保险箱。

输入/输出都做字段、长度、版本、重复JSON键及严格类型校验。父进程只允许固定上游错误码；stderr丢弃、不把原始异常/HTTP正文回传，trace沿用状态/原因/计数/耗时。协议正确也不代表引用可靠，最终连续原文与来源权限校验仍由父进程执行。

## 故障与业务结果

| reason | 含义/处理 |
| --- | --- |
| generation_deadline_exceeded | 预算内未得到完整且按时退出的结果；空答案/无引用，不自动重试 |
| generation_worker_busy | 正常在用或隔离占满容量；无需等待启动另一个进程 |
| generation_worker_start_failed | 无法启动固定进程或管道线程；不切回直接网络调用 |
| generation_worker_failed | 非零退出或管道失败；无部分选摘 |
| generation_worker_response_limit / protocol_error | 包超限、歧义或结构不符；拒绝结果 |
| generation_worker_cleanup_failed | 未确认进程/I/O结束；保留隔离名额，排查部署环境/受控重启 |
| generation_worker_input_invalid | 内部配置/上下文违反worker预算；不启动进程 |

原upstream_http_error、upstream_response_limit等静态原因保留。若同时检测到资料/权限修订变化，原knowledge_changed复核可能优先覆盖业务原因，仍清空旧证据；worker占用状态独立保留。超时不证明服务商停止计算或不计费，不能自动重试掩盖不确定性。

## 可复现实验与证据

```bash
python -X utf8 -m compliance_rag.generation_demo
python -X utf8 -m pytest -q
```

generation_demo运行真实Python子进程、真实回环HTTP模拟服务、真实SQLite及FastAPI路径。**API传输是进程内ASGI**，模拟服务只复制原创合成证据，不执行LLM；不要把它称为真实模型联调。输出与[固定报告](../examples/evaluation/generation-worker.json)一致：

1. 正常协议经子进程返回180天原文引用，子进程/I/O结束后占用归零。
2. 模拟服务每50ms发一个字节，远小于原30秒read timeout；仍在6秒生成预算耗尽时拒答，重叠API请求429，答案/引用为空。
3. 确认占用和隔离数都归零，trace只记录静态deadline原因；下一正常请求再次成功。

专项测试另覆盖真实服务一直不发响应头、真实子进程不读取stdin、非零退出、启动/线程失败、输出包超限、重复JSON/错误版本/额外字段、迟到成功拒绝，以及回收/退出检查不确定时隔离保留和恢复。时间参数是功能实验预算，不是吞吐/延迟性能承诺。没有真实生成服务、费用或语义质量实测；原BGE和20问开发集的历史结果仍独立列出。

## 仍未完成

- 仅监督外部选摘；慢请求体、BGE/SQLite、全请求硬截止与独立持久任务队列仍未解决。
- 强制杀死父进程/机器崩溃时不保证子进程自动消失，没有Windows Job Objects、容器/cgroup或进程树恢复；正常路径的回收验证不是崩溃持久保证。不得通过按进程名批量杀进程来补救。
- 子进程不是权限/内存/CPU沙箱；恶意本机写者、被篡改的Python/包目录不在防护承诺内。
- 已发到服务商的材料不能撤回，远端任务和计费未受本地kill控制；来源授权的检查后发送窗口仍在。
- 无真实IdP、令牌在线撤销、操作者审计、费用预算或独立人工标注集；当前不等于生产等保系统。

依据：[HTTPX timeout语义](https://www.python-httpx.org/advanced/timeouts/)、[Python subprocess生命周期](https://docs.python.org/3.12/library/subprocess.html#subprocess.Popen.communicate)、[Event等待](https://docs.python.org/3.12/library/threading.html#event-objects)。具体双管道线程/容量隔离为本项目设计，不由这些文档直接推出生产安全或时间SLA。
