# search-tool.md

代码搜索工具（ace-tool MCP 优先）

## 指导原则

- 涉及代码检索、实现定位、调用链理解、架构探索时，默认先用 `search_context`。
- 进入未知代码区域时，第一步必须使用 `search_context` 建立上下文。
- 查询建议使用“自然语言意图 + 关键词”，优先复用用户措辞。
- 未先使用 `search_context` 就直接做代码级搜索，视为不合规。
