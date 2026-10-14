# 第一阶段验证记录

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
