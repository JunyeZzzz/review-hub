# 流水线节点契约（2026-10-28）

## 执行与兼容

全局和项目编排均是有序节点列表。旧 `kind: reviewer` 和 `kind: gate` 原样读取；新节点增加 `kind: api`。节点可设为“产出审查发现”或“提供后续上下文”。前者必须返回 `findings` JSON，参与交叉裁决；后者的文本传给后续节点，不参与裁决。启用的节点按列表顺序执行；门禁失败停止后续节点。风险路由和密钥扫描仍在列表之前执行，风险等级只标记报告，不隐式跳过模型节点。报告中保留完整执行记录。旧版没有显式门禁的已存编排在升级时一次性写入原先隐式执行的测试门禁；此后删除所有门禁不会再次自动补入，保存结果与界面看到的列表一致。

后续模型节点的输入包含基础审查提示、节点指令和**所有已执行前序节点的最终输出**。API 节点还内联 diff 和评审规则，因为它不能读取服务器文件。输入超过 500,000 字符或模型输出超过 200,000 字符时节点明确失败，避免静默截断；API 调用超时为 180 秒，响应上限 2 MB。模型 API 节点是一次文本调用，不具备 Agent 的仓库读取工具；需要读源码验证的任务应使用 Agent 节点。每次运行保存不含密钥的编排快照，步骤记录模型节点的完整输入和结果；超长门禁日志在报告中明确标出截断。修改编排在下一次审查生效。

例如：`测试门禁 → API 风险摘要（context）→ Agent 源码核验（findings）→ API 独立复核（findings）`。第三个节点会收到门禁日志和 API 摘要；第四个节点还会收到 Agent 的最终发现。只有后两个节点的发现参与裁决。

回归评测按同一项目编排运行节点；门禁结果供后续节点读取，但不计入命中率。已确认问题保存为“应报出”样本以检测漏报；已确认误报保存为“不应再报”样本以检测误报重现。误报可由人工选择同时写入“不再报告”规则。样本不会自动训练模型。

## 模型凭证

API 节点按模型凭证 ID 绑定。一个服务商可保存多组凭证；密钥写入权限为 `0600` 的独立文件，只在发起请求时读取，不进入 API 响应、流水线配置或运行快照。停用的凭证无法新增绑定，已绑定节点运行时明确失败；删除仍被编排引用的凭证会被拒绝。自定义服务必须使用 HTTPS Base URL，不接受本机或内网 IP 地址。服务商预设覆盖 OpenAI、Anthropic、Gemini、DeepSeek、Moonshot、Qwen、GLM、SiliconFlow、OpenRouter、Groq、Mistral、Azure OpenAI 和 Amazon Bedrock API Key；后两者须填写账户专属 Base URL。模型 ID 由节点指定，预设不承诺任何具体模型一定可用。Bedrock 的 AWS SigV4 凭证暂不支持。

端点参考：[OpenAI Chat](https://developers.openai.com/api/reference/chat-completions/overview)、[Anthropic Messages](https://platform.claude.com/docs/en/api/overview)、[Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai/)、[DeepSeek](https://api-docs.deepseek.com/quick_start/pricing/)、[Qwen Base URL](https://help.aliyun.com/en/model-studio/base-url)、[SiliconFlow Chat](https://docs.siliconflow.cn/docs/api/chat-completions-post)、[OpenRouter Chat](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)、[Groq](https://console.groq.com/docs/openai)、[Mistral](https://docs.mistral.ai/api)、[Azure OpenAI](https://learn.microsoft.com/en-us/azure/foundry/openai/latest)、[Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html)。预设 Base URL 允许在新增凭证时按账户区域覆盖。

## 决策留痕

- 承诺：用户配置的每个节点有清楚作用、顺序和独立参数；后续节点能读取所有前序最终输出；凭证和项目显式绑定。
- 假设：单次审查最多 20 个节点、输入最多 500,000 字符；服务管理员可信，API 供应商模型与区域由管理员选择。
- 取舍：按顺序执行更容易复盘，但没有并行提速；超出上下文预算直接失败，避免产生看似完整的错误结论；API 节点没有仓库工具能力。
- 复查：若大仓库频繁触及输入上限、API 成本不可控，或需要分支/并行节点，应增加显式上下文选择、预算估算和 DAG 执行契约。
