# 流水线节点契约（2026-10-28）

## 执行与兼容

全局和项目编排均是有序阶段列表：任意数量的门禁和 LLM 上下文节点，最后是唯一的 `kind: review` 交叉评审组。评审组包含 Agent 或模型 API 评审器。门禁只决定是否继续：失败直接阻断，通过或跳过的日志留在报告，不输入模型。上下文节点输出文本，供后续上下文节点及评审组读取。评审组成员各自拿到**相同**的上游 LLM 上下文；成员按界面排列顺序执行，但不会读到彼此的输出。全部成员结束后才交叉裁决。风险路由和密钥扫描仍在列表之前执行。报告保留每个实际执行步骤的记录。

旧版平铺的 `reviewer` / `api` 发现节点读取时自动收拢到最后一个交叉评审组；原门禁与上下文节点的相对顺序保留。旧数据只读规范化，保存时才写入新格式；旧运行快照不改写。没有显式门禁的旧配置仍按先前迁移规则一次性补测试门禁。新配置允许删除全部门禁。唯一评审组必须启用且至少启用一个成员；总节点数含组成员最多 20。组内成员顺序只影响执行先后与报告顺序：裁决前会固定合并次序，回归配置指纹也忽略成员重排。

后续上下文节点和每位评审器的输入包含基础提示、节点指令与**所有已完成的上游 LLM 上下文输出**。API 节点还内联 diff 和评审规则，因为它不能读取服务器文件。输入超过 500,000 字符或模型输出超过 200,000 字符时节点明确失败，避免静默截断；API 调用超时为 180 秒，响应上限 2 MB。模型 API 节点是一次文本调用，不具备 Agent 的仓库读取工具；需要读源码验证的任务应使用 Agent 节点。每次运行保存不含密钥的编排快照，步骤记录模型节点的输入和结果。修改编排在下一次审查生效。

例如：`测试门禁 → Agent 项目背景（context）→ API 风险摘要（context）→ 交叉评审 [Claude Agent | Kimi Agent | API 复核]`。API 摘要可读取 Agent 项目背景。三个评审器都读取这两份上下文，但不会读取其他评审器的发现。

回归评测按同一项目编排运行节点；门禁失败记执行错误，其日志不进入上下文；评审器仍保持输入隔离。已确认问题保存为“应报出”样本以检测漏报；已确认误报保存为“不应再报”样本以检测误报重现。误报可由人工选择同时写入“不再报告”规则。样本不会自动训练模型。

## 模型凭证

API 节点按模型凭证 ID 绑定。一个服务商可保存多组凭证；密钥写入权限为 `0600` 的独立文件，只在发起请求时读取，不进入 API 响应、流水线配置或运行快照。停用的凭证无法新增绑定，已绑定节点运行时明确失败；删除仍被编排引用的凭证会被拒绝。自定义服务必须使用 HTTPS Base URL，不接受本机或内网 IP 地址。服务商预设覆盖 OpenAI、Anthropic、Gemini、DeepSeek、Moonshot、Qwen、GLM、SiliconFlow、OpenRouter、Groq、Mistral、Azure OpenAI 和 Amazon Bedrock API Key；后两者须填写账户专属 Base URL。模型 ID 由节点指定，预设不承诺任何具体模型一定可用。Bedrock 的 AWS SigV4 凭证暂不支持。

端点参考：[OpenAI Chat](https://developers.openai.com/api/reference/chat-completions/overview)、[Anthropic Messages](https://platform.claude.com/docs/en/api/overview)、[Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai/)、[DeepSeek](https://api-docs.deepseek.com/quick_start/pricing/)、[Qwen Base URL](https://help.aliyun.com/en/model-studio/base-url)、[SiliconFlow Chat](https://docs.siliconflow.cn/docs/api/chat-completions-post)、[OpenRouter Chat](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)、[Groq](https://console.groq.com/docs/openai)、[Mistral](https://docs.mistral.ai/api)、[Azure OpenAI](https://learn.microsoft.com/en-us/azure/foundry/openai/latest)、[Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html)。预设 Base URL 允许在新增凭证时按账户区域覆盖。

## 决策留痕

- 承诺：门禁只拦截，上下文只由上游 LLM 输出组成；交叉评审组成员独立判断并在组后裁决；旧配置可读且保存后规范化。
- 假设：单次审查最多 20 个节点、输入最多 500,000 字符；交叉评审不依赖成员间讨论，组内按序执行即可。
- 取舍：固定评审组为终点，简化输入隔离与复盘；接受串行执行耗时及不能在评审之后再加上下文节点。
- 复查：若需要评审后复核、并行提速或多轮讨论，另设计显式阶段与输入权限，避免靠卡片顺序暗示数据依赖。
