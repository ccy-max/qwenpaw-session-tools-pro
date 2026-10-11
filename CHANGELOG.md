# 更新日志

## v3.0.2 (2026-10-11)
- 修复：回退/删除/撤回后硬刷新仍可能残留旧气泡（宿主内存消息缓存活过 reload）
  —— 新增 purgeMessageCache：刷新前经 React fiber 遍历找到宿主消息 store，
  调 clearSessionMessages + setMessages([]) 清缓存后再 reload

## v3.0.1 (2026-10-11)
- 新增 `scripts/build_release.py`：构建插件安装包 zip（plugin.json 置于包根，含 README.md / LICENSE / CHANGELOG.md）
- 新增 GitHub Actions：推送 `v*` tag 自动构建 zip 并挂载到 Release
- 仓库规范：`dist/` 加入 .gitignore

## v3.0.0 (2026-10-11) — SessionToolsPro

基于上游 [qwenpaw-plugin-session-tools](https://platform.agentscope.io/plugins/qwenpaw-plugin-session-tools) v2.4.0 增强二开。

### 数据安全
- 破坏性操作前自动快照到 `sessions/.session-tools-pro-backups/`（每会话保留 20 份）
- 新增 `/undo` slash 命令与 `POST /session/{sid}/undo`、`GET /session/{sid}/snapshots` HTTP 端点；撤销操作本身亦有快照（可撤销撤销）
- `_update_content` 增加 `expect_count` 乐观并发校验，冲突返回 409，杜绝操作期间 agent 追加消息被静默覆盖
- `regen` 移除"先删会话文件、再 DOM 模拟点击发送"链路，改为删除+回填输入框由用户确认发送
- fork 的 HTTP 路径删除手写 `chats.json` 分支，与 slash 统一走 `ChatManager.create_chat`

### 前端重写
- `confirm()`/`alert()`/emoji → 宿主 antd Popconfirm / notification / @ant-design/icons，跟随主题
- 按钮经 `item.render` 整体接管，自带 loading 防重复提交
- 操作结果通知带「撤销」按钮；回填文本、撤销通知、分叉跳转经 sessionStorage 跨硬刷新传递
- 用户气泡补齐「撤回/删除」，与助手气泡按钮集与顺序一致（撤回、删除、回退、分叉）
- `message_text` 截断 200 字符，修复长回复分叉时 URL 超请求行上限被 uvicorn 拒 400
- 移除对不存在的 `window.currentUserId/currentChannel` 的依赖，改由后端扫描会话目录反查

### 行为修正
- 删除改为轮次感知：删整轮（user + assistant + system 工具链）
- 回退/撤回一律回填本轮用户提问，不再错误回填 AI 回复文本
- 消息定位三层兜底：original_id → created_at → 文本前缀+角色

### 仓库规范
- 后端文件移入 `backend/`、测试移入 `tests/`（宿主嵌套入口原生支持）
- tag 推送自动触发 GitHub Actions 构建安装包并挂载 Release

## v2.4.0 — 上游基线（one-sixth）
- 初版能力：/rewind /fork /regen /noreply、气泡按钮、created_at/original_id 定位
