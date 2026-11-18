"""平台层：队列 / 存储 / 调度 / 浏览器 / 限流。

**依赖规则**（ADR §1.3）：`platform` 不得 import `services`。
`platform` 只提供与业务无关的基础设施能力。
"""
