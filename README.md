# ⚡ 会话增强 Pro (SessionToolsPro)

> 会话管理增强插件（v3.0.0）｜插件 ID：`session-tools-pro`
> 在 QwenPaw 会话里为**每条消息气泡**挂上「撤回 / 删除 / 回退 / 分叉」操作，并补齐 `/undo` 反悔能力——所有破坏性操作**先快照后写入**，误删一键还原，**零重启热安装**。
>
> - **本项目基于 [qwenpaw-plugin-session-tools](https://platform.agentscope.io/plugins/qwenpaw-plugin-session-tools)（作者 [one-sixth](https://github.com/one-sixth)，v2.4.0）增强二次开发**，保留原版全部能力，重写前端交互并加固数据安全性，差异见[版本更新摘要](#版本更新摘要)
> - **适用版本**：QwenPaw 2.0.0 ~ 2.3.0
> - **授权**：MIT（上游亦为 MIT，见 [LICENSE](LICENSE)）

---

## 目录

- [功能特性](#功能特性)
- [安装](#安装)
- [卸载](#卸载)
- [使用说明](#使用说明)
- [控制台命令](#控制台命令)
- [HTTP API](#http-api)
- [架构与关键设计](#架构与关键设计)
- [常见问题](#常见问题)
- [版本更新摘要](#版本更新摘要)
- [来源与致谢](#来源与致谢)

---

## 功能特性

### 核心能力

| 能力 | 说明 |
|------|------|
| **快照安全网** | 每个破坏性操作（回退/删除/撤回/分叉源修改）写入前先快照会话文件，`/undo` 或通知里的「撤销」按钮一键还原 |
| **并发防覆盖** | 写回前 `expect_count` 乐观校验：操作期间 agent 若追加了消息，中止写入返回 409，**绝不静默覆盖新消息** |
| **轮次感知删除** | 点任意一条消息删**整轮**（用户提问 + 全部 assistant/system 工具链），不再只删"相邻一对" |
| **撤回并编辑** | 删除本轮并把原提问回填输入框，改完手动发送——不替用户做主，不模拟点击 |
| **分叉即跳转** | 从任意消息处复制出新会话（含到该消息的全部上下文），注册进会话列表并自动跳转 |
| **三层定位兜底** | `original_id` 精确 → `created_at` 时间 → 文本前缀+角色，任一可用即可定位，按钮不罢工 |
| **多 agent 感知** | 按请求解析 agent workspace，`user_id/channel` 缺省时自动扫描会话目录反查 |
| **antd 原生 UI** | Popconfirm 内联确认、notification 结果通知、`@ant-design/icons` 图标，全部跟随宿主主题 |

### 消息气泡按钮（用户与助手消息一致，顺序相同）

| 按钮 | 图标 | 行为 | 可撤销 |
|------|------|------|--------|
| 撤回并编辑 | ↩ | 删除本轮（提问+回复），原文回填输入框 | ✅ |
| 删除本轮 | 🗑 | 删除该消息所在整轮 | ✅ |
| 回退 | ⏪ | 删除该消息**及之后**全部内容 | ✅ |
| 分叉 | 🍴 | 从该消息（含）向前复制为新会话 | —（非破坏） |

> 🔍📄 调试按钮（探测 ctx.data / 查看消息 JSON）默认隐藏，开启：`localStorage.setItem('SessionToolsPro.debug', 'true')` 后刷新。

---

## 安装

```bash
git clone https://github.com/ccy-max/qwenpaw-session-tools-pro.git
qwenpaw plugin install ./qwenpaw-session-tools-pro --force
```

QwenPaw 运行中会**热加载**（无需重启）。装完**硬刷新控制台（Ctrl+Shift+R）**让前端脚本生效。

验证：

```bash
qwenpaw plugin list   # 应看到 会话增强 Pro (v3.0.0)
```

> ⚠️ 若装过上游原版 `qwenpaw-plugin-session-tools`，**必须先卸载**再装本插件——两者 slash 命令（`/rewind` 等）与 HTTP 前缀重叠，共存会注册冲突。

## 卸载

```bash
qwenpaw plugin uninstall session-tools-pro
```

> 快照目录 `sessions/.session-tools-pro-backups/` 卸载时不会自动删除，确认不再需要反悔后手动清理即可。

---

## 使用说明

1. 硬刷新控制台，任意消息气泡下方出现 4 个操作图标
2. 点击后弹 **Popconfirm 内联确认**（防误触），确认后执行
3. 操作完成 → 页面自动刷新同步（宿主消息 store 为私有 React 状态，刷新是唯一可靠同步手段，见[常见问题](#常见问题)）
4. 右下角通知带 **「撤销」按钮**：点一下即还原到操作前快照（12 秒内有效；过期可用 `/undo`）
5. 撤回/回退后，本轮原提问已**回填输入框**，改完手动发送即可

## 控制台命令

| 命令 | 功能 | 说明 |
|------|------|------|
| `/rewind [N]` | 回退 N 轮对话 | 默认 1 轮，自动快照可 `/undo` |
| `/fork [N]` | 分叉新会话 | 从 N 轮前分叉（默认 0 = 完整复制） |
| `/regen` | 撤回本轮重发 | 删除最后一轮，原提问替换掉 `/regen` 继续发送 |
| `/noreply 消息` | 静默记录 | 写入会话但不触发 AI 回复 |
| `/undo [快照ID]` | **还原快照** | 缺省还原最近一份；可先查 `/session-tools/session/{id}/snapshots` |

```
/rewind         → 回退 1 轮
/fork 2         → 从 2 轮前分叉
/noreply 收到   → 记录"收到"但不回复
/undo           → 反悔上一次操作
```

## HTTP API

挂载于 `/api/session-tools/`（鉴权同宿主 API，带 `X-Agent-Id` 头）：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/session/{sid}/rewind` | 回退到定位消息处，返回 `rewound_message` + `undo_id` |
| POST | `/session/{sid}/regen` | 撤回本轮，返回原提问 + `undo_id` |
| POST | `/session/{sid}/fork` | 分叉，返回 `new_session_id` / `registered` |
| DELETE | `/session/{sid}/message` | 轮次感知删除，返回 `deleted` + `undo_id` |
| POST | `/session/{sid}/undo` | 还原快照（`snapshot_id` 缺省 = 最近一份） |
| GET | `/session/{sid}/snapshots` | 列出快照 ID（新→旧） |
| GET | `/session/{sid}/message` | 消息详情（调试用） |
| GET | `/session/{sid}/info` | 会话统计 |

定位参数三选一（按优先级）：`original_id` > `created_at` > `message_text`+`role`；`user_id`/`channel` 可省略。

---

## 架构与关键设计

```
session-tools-pro/
├── plugin.json        # 插件元数据（id: session-tools-pro，入口指向 backend/）
├── backend/
│   ├── plugin.py      # slash 命令 + HTTP API + 快照/undo 编排
│   └── session_ops.py # 核心：会话文件读写、消息定位、snapshot/restore
├── frontend/
│   └── index.js       # 气泡按钮（render 整体接管）、通知撤销、跨刷新状态
└── tests/
    ├── conftest.py    # 把 backend/ 加入 sys.path
    └── test_find_message.py
```

### 消息定位（三层兜底）

```
ctx.data.input[0].metadata.original_id  (用户气泡)  ┐
ctx.data.output[0].metadata.original_id (助手气泡)  ┴→ ① msg.id 精确匹配
ctx.data.created_at (Unix 秒)                        → ② 时间匹配（严格→±1s 容差）
气泡渲染文本（截断 200 字符）+ role                   → ③ 前缀 50 字符 + 时间 + 邻居消歧
```

第③层文本只取前 200 字符——后端本就只比对前 50 字符前缀，且整段长文本进 query string 会撑爆请求行（uvicorn 直接 400，见 v3.0.0 修复记录）。

### 快照与撤销

- 快照位置：`{workspace}/sessions/.session-tools-pro-backups/{channel}/{会话文件名}/{时间戳}-{操作名}.json`
- 每个会话保留最近 **20** 份，超出自动修剪
- 撤销本身也是一次写操作，会先做 `pre-restore` 快照——**撤销撤销操作**同样可恢复

### 并发防覆盖

`_update_content(expect_count=N)`：写回前重读磁盘，消息总数 ≠ N 即判定「读取→写回」窗口内有并发写入（典型场景：agent 正在流式追加），抛 `SessionChangedError` → HTTP 409，前端提示重试。

### 刷新与跨刷新状态

破坏性操作后页面**硬刷新**（350ms 延迟）保证 DOM 与文件同步——宿主消息列表是模块私有的 zustand store，插件 API 面无任何写入口，外部改 DOM 会被下次 re-render 复活甚至弄崩 reconciler。刷新会丢失的三样东西经 `sessionStorage` 传递、加载时还原：

| 状态 | 还原动作 |
|------|---------|
| 回填输入框文本 | 轮询等待 React 挂载就绪后 nativeInputValueSetter 填入 |
| 撤销通知 | 重新弹出带「撤销」按钮的 notification |
| 分叉跳转目标 | 侧栏列表拉出后派发 `qwenpaw:sidebar-select-session` 跳入新会话 |

### 会话文件

```
{workspace_dir}/sessions/{channel}/{user_id}_{sanitized_session_id}.json
```

兼容新旧两种格式：新 `agent.state.context`（Msg dict 列表）、旧 `agent.memory.content`（`[msg_dict, extra]` 对列表），自动检测。

---

## 常见问题

**Q：为什么点完按钮页面会刷新一下？**
宿主的消息渲染数据在插件不可达的 React store 里，只改文件不改 store 会造成"页面看着还在、刷新后消失"的假象；外部直接删 DOM 节点会被 React 下次渲染复活甚至报错。硬刷新 + sessionStorage 状态传递是目前宿主架构下唯一可靠的同步方式。

**Q：误删了怎么办？**
通知里点「撤销」（12 秒内），或输入 `/undo` 还原最近快照，或 `/undo <快照ID>` 还原指定版本（`GET /api/session-tools/session/{sid}/snapshots` 可查）。快照在 agent 重启后依然有效。

**Q：分叉报 `Unexpected token '<'`？**
v3.0.0 已修复（长文本截断防 URL 超长）。若仍出现，确认前端脚本是新版：硬刷新后 F12 Console 应有 `[SessionToolsPro] 按钮注册完成`。

**Q：agent 正在回复时点了按钮会怎样？**
写入前并发校验发现消息数变了 → 409 中止，你的新消息不会被覆盖，等回复完成后重试即可。

**Q：原版能和本插件共存吗？**
不能，slash 命令与 HTTP 前缀冲突，先 `qwenpaw plugin uninstall qwenpaw-plugin-session-tools`。

**Q：改了插件代码不生效？**
`qwenpaw plugin install <路径> --force` 热重装 + 浏览器 Ctrl+Shift+R 硬刷新（前端 JS 有缓存）。

---

## 版本更新摘要

### v3.0.0 (2026-10-11) — SessionToolsPro 首个版本（基于上游 v2.4.0）

**数据安全**
- 破坏性操作前自动快照到 `sessions/.session-tools-pro-backups/`（每会话 20 份），新增 `/undo` 命令与 HTTP undo 端点；撤销操作本身也有快照
- `_update_content` 增加 `expect_count` 乐观并发校验，冲突返回 409，杜绝"操作期间 agent 追加消息被静默覆盖"
- `regen` 移除"先删会话文件、再 DOM 模拟点击发送"链路——改为删除+回填输入框由用户确认发送，删文件与发送失败不再可能丢数据
- fork 的 HTTP 路径删除手写 `chats.json` 分支，与 slash 统一走 `ChatManager.create_chat`，不再与运行中的 ChatManager 互相覆盖

**前端重写**
- `confirm()`/`alert()`/emoji → 宿主 `antd` Popconfirm / notification / `@ant-design/icons`，跟随主题
- 按钮经 `item.render` 整体接管（宿主 Footer.Actions 对 render 项只 `cloneElement(key)` 不绑 onClick），自带 loading 防重复提交
- 操作结果通知带「撤销」按钮；回填文本、撤销通知、分叉跳转经 sessionStorage 跨硬刷新传递
- 用户气泡补齐「撤回/删除」，与助手气泡按钮集与顺序一致
- `message_text` 截断 200 字符，修复长回复分叉时 URL 超请求行上限被 uvicorn 拒 400（前端解析 HTML 报 `Unexpected token '<'`）
- 移除对不存在的 `window.currentUserId/currentChannel` 的依赖，`user_id/channel` 改由后端扫描会话目录反查

**行为修正**
- 删除改为**轮次感知**：删整轮（user + assistant + system 工具链），原版只删"相邻一对"
- 回退/撤回一律回填**本轮用户提问**，原版点助手气泡会错误回填 AI 回复文本
- 消息定位三层兜底（id → 时间 → 文本+角色），原版 id/时间失配即按钮失效

### v2.4.0（上游基线，one-sixth/qwenpaw-plugin-session-tools）
- 初版能力：/rewind /fork /regen /noreply、气泡按钮、created_at/original_id 定位

---

## 来源与致谢

- 本项目是 **[qwenpaw-plugin-session-tools](https://platform.agentscope.io/plugins/qwenpaw-plugin-session-tools)**（作者 [one-sixth](https://github.com/one-sixth)，MIT）的增强分叉（forked from v2.4.0）。git 历史保留了完整上游基线：`v2.4.0-baseline` tag 即原版源码，`v3.0.0` 起为 Pro 改造，`git diff v2.4.0-baseline v3.0.0` 可查看全部差异。
- 感谢上游提供优秀的起点；本仓库的改动集中在数据安全（快照/并发校验/轮次删除）与前端交互（antd 化/可撤销/不模拟发送）。

## 📄 许可

MIT License — 见 [LICENSE](LICENSE)

## 👥 作者

- **ccy-max (Zero)** ｜ https://github.com/ccy-max
