/**
 * 会话增强 Pro — 前端 UI（QwenPaw 2.x / session-tools-pro 3.0.0）
 *
 * 相对原版 2.4.0 的变化：
 * - confirm()/alert() → antd Popconfirm / notification（host.antd，跟随主题）
 * - emoji → @ant-design/icons（host.antdIcons）
 * - 所有按钮走 render 整体接管（宿主 Footer.Actions 对 item.render 只
 *   cloneElement(key)，不绑 onClick —— 点击行为完全由我们渲染的组件负责）
 * - 破坏性操作成功后：先从 React fiber 摸到宿主消息 store 清缓存
 *   （purgeMessageCache，防旧气泡活过刷新），再硬刷新保证 DOM 与文件
 *   同步（宿主对"切到当前会话"是 no-op，软刷新不可靠）；回填输入框 /
 *   撤销通知 / fork 后跳新会话这些意图经 sessionStorage 传递，
 *   刷新后由 restorePending 还原
 * - regen → 改为「撤回本轮并编辑」：后端删除该轮，原文回填输入框，
 *   由用户确认后手动发送 —— 删掉脆弱的 DOM 模拟发送（Enter+click）
 * - 不再依赖不存在的 window.currentUserId / currentChannel；
 *   user_id/channel 干脆不传，后端扫描 sessions/ 反查
 * - 定位三层兜底：original_id → created_at → 文本+角色
 *
 * 调试：localStorage.setItem('SessionToolsPro.debug','true') 后刷新，
 * 出现 🔍（ctx.data 探测）和 📄（消息 JSON）按钮。
 */
(function () {
  "use strict";

  var GROUP = "SessionToolsPro";
  var API_PREFIX = "/session-tools";

  function host() {
    return (window.QwenPaw && window.QwenPaw.host) || {};
  }
  function h() {
    var R = host().React;
    if (!R) throw new Error("host.React unavailable");
    return R;
  }
  var DEBUG = localStorage.getItem(GROUP + ".debug") === "true";

  // ── 基础工具 ─────────────────────────────────────────────

  function getApiUrl(path) {
    try {
      if (host().getApiUrl) return host().getApiUrl(path);
    } catch (_) {}
    return window.location.origin + "/api" + path;
  }

  function getApiToken() {
    try {
      if (host().getApiToken) return host().getApiToken() || "";
    } catch (_) {}
    return "";
  }

  /** 当前会话 ID：宿主 runtime API → URL 兜底 → 旧全局兜底 */
  function getSessionId() {
    var hh = host();
    try {
      if (hh.getCurrentSessionId) {
        var sid = hh.getCurrentSessionId();
        if (sid) return sid;
      }
    } catch (_) {}
    var m = window.location.pathname.match(/\/chat\/([^/?#]+)/);
    if (m && m[1] !== "new") return m[1];
    return window.currentSessionId || "";
  }

  async function callApi(method, path, params) {
    var url = getApiUrl(path);
    var qs = [];
    if (params) {
      for (var k in params) {
        if (params.hasOwnProperty(k) && params[k] !== undefined && params[k] !== null && params[k] !== "") {
          qs.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(params[k])));
        }
      }
    }
    if (qs.length) url += "?" + qs.join("&");

    var headers = { "Content-Type": "application/json" };
    var token = getApiToken();
    if (token) headers["Authorization"] = "Bearer " + token;
    try {
      if (host().getSelectedAgentId) {
        var agentId = host().getSelectedAgentId();
        if (agentId) headers["X-Agent-Id"] = agentId;
      }
    } catch (_) {}

    try {
      var resp = await fetch(url, { method: method, headers: headers });
      return await resp.json();
    } catch (e) {
      console.error("[" + GROUP + "] API call failed:", e);
      return { success: false, detail: "网络错误: " + e.message };
    }
  }

  // ── antd 桥接 ────────────────────────────────────────────

  function antd() {
    return host().antd || {};
  }
  function icons() {
    return host().antdIcons || {};
  }

  function toast(text, type) {
    var a = antd();
    if (a.message && a.message[type]) { a.message[type](text); return; }
    if (a.message) { a.message(text); return; }
    console.log("[" + GROUP + "] " + (type || "info") + ": " + text);
  }

  /** 成功通知，可带「撤销」按钮 */
  function notifySuccess(title, description, onUndo) {
    var a = antd();
    if (!a.notification || !a.notification.open) { toast(title + (description ? " — " + description : ""), "success"); return; }
    var React = h();
    var Button = a.Button;
    var opts = {
      message: title,
      description: description || undefined,
      placement: "bottomRight",
      duration: onUndo ? 12 : 4,
    };
    if (onUndo && Button) {
      opts.btn = React.createElement(Button, {
        size: "small",
        danger: true,
        type: "primary",
        onClick: function () {
          a.notification.destroy("stpro-undo");
          onUndo();
        },
      }, "撤销（还原快照）");
      opts.key = "stpro-undo";
    }
    a.notification.open(opts);
  }

  function errText(result, fallback) {
    if (!result) return fallback;
    return result.detail || (result.success === false ? JSON.stringify(result) : fallback);
  }

  // ── 刷新 + 跨刷新状态传递 ────────────────────────────────
  //
  // 宿主的侧栏切换事件对"切到当前会话"是 no-op（路由判定没换会话，
  // 不会重新拉消息 store），所以同会话的破坏性操作只能硬刷新让 DOM
  // 与文件同步。为了刷新后不丢"回填输入框 / 撤销通知 / 跳转新会话"，
  // 把这些意图存进 sessionStorage，刷新后由 restorePending 还原。

  var PENDING_KEY = GROUP + ".pending";
  var PENDING_TTL = 20000; // 20s 内有效，过期丢弃避免陈旧还原

  /**
   * 清掉宿主内存里的消息缓存（zustand messages store）。
   *
   * 背景：宿主把消息放在 React Context（含 setSessionMessages /
   * clearSessionMessages），location.reload 前如果不清，某些场景下
   * 内存里的旧气泡能活过刷新（用户实测：回退后"好的"残影）。
   * 这里从 React 根 fiber 往下找携带 clearSessionMessages 的
   * Provider value，找到就清当前会话 + 清全局视图，best-effort。
   */
  function purgeMessageCache(sessionId) {
    try {
      var rootEl = document.getElementById("root") || document.body.firstElementChild;
      if (!rootEl) return false;
      var key = Object.keys(rootEl).find(function (k) {
        return k.indexOf("__reactContainer") === 0 || k.indexOf("__reactFiber") === 0;
      });
      if (!key) return false;

      var purged = false;
      var seen = 0;
      (function walk(fiber) {
        if (!fiber || seen++ > 4000 || purged) return;
        var props = fiber.memoizedProps;
        if (props && typeof props === "object") {
          var v = props.value;
          if (v && typeof v.clearSessionMessages === "function" &&
              typeof v.setMessages === "function") {
            try {
              // 清每个已知会话的缓存 + 当前视图
              v.clearSessionMessages(sessionId || "");
              if (sessionId && typeof v.getSessionMessages === "function") {
                // 某些实现 clear 需要逐会话调；把当前缓存里的 session 一并清
                v.clearSessionMessages(undefined);
              }
              v.setMessages([]);
              purged = true;
            } catch (_) {}
            return;
          }
        }
        walk(fiber.child);
        var sibling = fiber.sibling;
        while (sibling && !purged) { walk(sibling); sibling = sibling.sibling; }
      })(rootEl[key]);

      if (DEBUG) console.log("[" + GROUP + "] purgeMessageCache:", purged);
      return purged;
    } catch (e) {
      if (DEBUG) console.warn("[" + GROUP + "] purge failed:", e);
      return false;
    }
  }

  function scheduleReload(payload) {
    try {
      payload.ts = Date.now();
      sessionStorage.setItem(PENDING_KEY, JSON.stringify(payload));
    } catch (_) {}
    // 刷新前先清宿主内存消息缓存，防止旧气泡活过 reload
    purgeMessageCache(payload && payload.sessionId);
    setTimeout(function () { window.location.reload(); }, 350);
  }

  function consumePending() {
    var raw;
    try {
      raw = sessionStorage.getItem(PENDING_KEY);
      if (!raw) return null;
      sessionStorage.removeItem(PENDING_KEY);
    } catch (_) { return null; }
    try {
      var p = JSON.parse(raw);
      if (!p || (Date.now() - (p.ts || 0)) > PENDING_TTL) return null;
      return p;
    } catch (_) { return null; }
  }

  /** 通过宿主侧栏机制跳转到某会话（跨会话场景，列表须已含该会话） */
  function switchToSession(sessionId) {
    window.dispatchEvent(new CustomEvent("qwenpaw:sidebar-select-session", {
      detail: { sessionId: sessionId },
    }));
  }

  /** 轮询等 React 挂载好输入框再回填 */
  function fillWhenReady(text) {
    var tries = 0;
    (function attempt() {
      if (putIntoSender(text)) return;
      if (tries++ < 25) setTimeout(attempt, 150);
    })();
  }

  /** 刷新后还原：回填输入框、弹撤销通知、跳转 fork 新会话 */
  function restorePending(p) {
    if (!p) return;
    if (p.text) fillWhenReady(p.text);
    if (p.notify) {
      notifySuccess(p.notify.title, p.notify.desc,
        p.notify.undoId ? function () { doUndo(p.sessionId, p.notify.undoId); } : null);
    }
    if (p.navigateTo) {
      // 等侧栏列表刷新出来后跳转；多次重试派发事件
      var tries = 0;
      (function go() {
        switchToSession(p.navigateTo);
        if (tries++ < 8 && getSessionId() !== p.navigateTo) setTimeout(go, 400);
      })();
    }
  }

  // ── 输入框回填 ───────────────────────────────────────────

  function putIntoSender(text) {
    var el = document.querySelector("textarea.qwenpaw-sender-input") ||
             document.querySelector(".qwenpaw-sender-input textarea") ||
             document.querySelector("textarea");
    if (!el) return false;
    try {
      var setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
      setter.call(el, text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.focus();
      return true;
    } catch (_) {
      return false;
    }
  }

  // ── ctx.data 定位信息提取 ────────────────────────────────

  function blocksText(blocks) {
    if (!blocks) return "";
    if (typeof blocks === "string") return blocks;
    if (Array.isArray(blocks)) {
      for (var i = 0; i < blocks.length; i++) {
        var b = blocks[i];
        if (b && b.type === "text" && typeof b.text === "string") return b.text;
      }
    }
    return "";
  }

  /**
   * 从 ctx.data 提取定位三件套 + 角色
   * 用户气泡: ctx.data.input[]；助手气泡: ctx.data.output[]
   */
  function getLocator(ctx) {
    var data = (ctx && ctx.data) || {};
    var msg = null, role = "";
    if (data.input && data.input[0]) { msg = data.input[0]; role = "user"; }
    else if (data.output && data.output[0]) { msg = data.output[0]; role = "assistant"; }
    var meta = (msg && msg.metadata) || {};
    // message_text 仅用于后端"前缀+时间+邻居"兜底定位，后端只比对前 50 字符；
    // 整段塞进 query string 会让长回复（几千字）撑爆请求行被 uvicorn 拒 400
    // （浏览器收到 HTML 错误页 → "Unexpected token '<'"），故截断到 200。
    var text = blocksText(msg && msg.content);
    return {
      original_id: meta.original_id || "",
      created_at: data.created_at !== undefined && data.created_at !== null ? String(data.created_at) : "",
      message_text: text.length > 200 ? text.slice(0, 200) : text,
      role: role,
    };
  }

  // ── 按钮基座（render 整体接管） ──────────────────────────

  /**
   * 生成一个 React 组件（函数组件），渲染 antd Tooltip(+Popconfirm)+Button。
   *
   * cfg: { title, confirm, danger, icon(name), isDebug, action(ctxData, done) }
   *   - confirm 为字符串时点击先弹 Popconfirm，确认后才执行 action
   *   - action 收到 locator 相关 ctx，自行调 API 并调用 done(result)
   *   - 组件内部维护 loading，防重复提交
   */
  function makeButton(cfg) {
    var React = h();
    var a = antd();
    var ic = icons();
    var IconComp = ic[cfg.icon] || null;

    var Tooltip = a.Tooltip;
    var Popconfirm = a.Popconfirm;
    var Button = a.Button;

    return function StProButton(props) {
      var ctxData = props && props.data;
      var loadingState = React.useState(false);
      var loading = loadingState[0], setLoading = loadingState[1];
      var openState = React.useState(false);
      var popOpen = openState[0], setPopOpen = openState[1];

      function run() {
        if (loading) return;
        setLoading(true);
        var done = function () { setLoading(false); setPopOpen(false); };
        try {
          cfg.action({ data: ctxData, locator: getLocator({ data: ctxData }) }, done);
        } catch (e) {
          console.error("[" + GROUP + "] action error:", e);
          toast("操作异常: " + e.message, "error");
          done();
        }
      }

      // 无 onClick 兜底时（组件自管），loading 需自行超时复位：
      // action 内部必须调用 done；若忘记，15s 兜底复位
      React.useEffect(function () {
        if (!loading) return;
        var t = setTimeout(function () { setLoading(false); }, 15000);
        return function () { clearTimeout(t); };
      }, [loading]);

      var iconNode = IconComp
        ? React.createElement(IconComp, { spin: cfg.icon === "SyncOutlined" && loading })
        : React.createElement("span", { style: { fontSize: 14 } }, cfg.fallbackEmoji || "•");

      var btnProps = {
        type: "text",
        size: "small",
        icon: iconNode,
        danger: !!cfg.danger,
        style: cfg.color ? { color: cfg.color } : undefined,
        loading: loading,
        disabled: !cfg.alwaysOn === false && false,
        onClick: cfg.confirm ? undefined : run,
      };
      if (cfg.confirm) {
        // Popconfirm 受控：渲染在 Button 外层，由 Button onClick 打开
        btnProps.onClick = function () { setPopOpen(true); };
      }

      var buttonNode = Button
        ? React.createElement(Button, btnProps)
        : React.createElement("button", {
            onClick: btnProps.onClick,
            style: { background: "none", border: "none", cursor: "pointer", padding: 4 },
          }, iconNode);

      var node = buttonNode;

      if (cfg.confirm && Popconfirm) {
        node = React.createElement(Popconfirm, {
          title: cfg.confirm,
          open: popOpen,
          onOpenChange: function (v) { if (!loading) setPopOpen(v); },
          onConfirm: run,
          okText: "确认",
          cancelText: "取消",
          okButtonProps: cfg.danger ? { danger: true } : undefined,
        }, buttonNode);
      }

      if (Tooltip && cfg.title) {
        node = React.createElement(Tooltip, { title: cfg.title }, node);
      }
      return node;
    };
  }

  // ── 各操作按钮 ───────────────────────────────────────────

  function apiPathFor(sessionId, suffix) {
    return API_PREFIX + "/session/" + encodeURIComponent(sessionId) + suffix;
  }

  function doUndo(sessionId, snapshotId) {
    callApi("POST", apiPathFor(sessionId, "/undo"), { snapshot_id: snapshotId || "" })
      .then(function (r) {
        if (r && r.success) {
          toast("↩️ 已还原到操作前状态", "success");
          scheduleReload({ sessionId: sessionId });
        } else {
          toast("还原失败: " + errText(r, "未知错误"), "error");
        }
      });
  }

  function rewindButton() {
    return {
      id: GROUP + ".rewind",
      render: makeButton({
        title: "回退：删除这条消息及之后所有（可撤销）",
        confirm: "删除这条消息之后的全部内容？",
        icon: "RollbackOutlined",
        fallbackEmoji: "⏪",
        action: function (args, done) {
          var sid = getSessionId();
          if (!sid) { toast("无法获取会话 ID", "error"); done(); return; }
          var p = { created_at: args.locator.created_at, original_id: args.locator.original_id,
                    message_text: args.locator.message_text, role: args.locator.role };
          callApi("POST", apiPathFor(sid, "/rewind"), p).then(function (r) {
            done();
            if (r && r.success) {
              scheduleReload({
                sessionId: sid,
                text: r.rewound_message || "",
                notify: {
                  title: "⏪ 已回退 " + r.deleted + " 条",
                  desc: r.rewound_message ? "本轮提问已放回输入框" : "会话已刷新",
                  undoId: r.undo_id || "",
                },
              });
            } else {
              toast("回退失败: " + errText(r, "未知错误"), "error");
            }
          });
        },
      }),
    };
  }

  function forkButton() {
    return {
      id: GROUP + ".fork",
      render: makeButton({
        title: "分叉：从这条消息（含）复制出新会话",
        confirm: "从此处分叉一个新会话？",
        icon: "ForkOutlined",
        fallbackEmoji: "🍴",
        action: function (args, done) {
          var sid = getSessionId();
          if (!sid) { toast("无法获取会话 ID", "error"); done(); return; }
          var p = { created_at: args.locator.created_at, original_id: args.locator.original_id,
                    message_text: args.locator.message_text, role: args.locator.role };
          callApi("POST", apiPathFor(sid, "/fork"), p).then(function (r) {
            done();
            if (r && r.success) {
              if (r.registered) {
                // 硬刷新让侧栏列表重新拉取（含新会话），刷新后再跳转
                scheduleReload({
                  sessionId: sid,
                  navigateTo: r.new_session_id,
                  notify: { title: "🍴 已分叉: " + r.session_name, desc: "已跳转到新会话", undoId: "" },
                });
              } else {
                notifySuccess("🍴 已分叉: " + r.session_name,
                  "⚠️ 未能注册到会话列表，请手动打开: " + (r.session_path || r.new_session_id));
              }
            } else {
              toast("分叉失败: " + errText(r, "未知错误"), "error");
            }
          });
        },
      }),
    };
  }

  function deleteButton() {
    return {
      id: GROUP + ".delete",
      render: makeButton({
        title: "删除本轮（提问+回复+工具链，可撤销）",
        confirm: "删除整轮对话？",
        icon: "DeleteOutlined",
        fallbackEmoji: "🗑",
        action: function (args, done) {
          var sid = getSessionId();
          if (!sid) { toast("无法获取会话 ID", "error"); done(); return; }
          var p = { created_at: args.locator.created_at, original_id: args.locator.original_id,
                    message_text: args.locator.message_text, role: args.locator.role };
          callApi("DELETE", apiPathFor(sid, "/message"), p).then(function (r) {
            done();
            if (r && r.success) {
              scheduleReload({
                sessionId: sid,
                notify: {
                  title: "🗑 已删除本轮 " + r.deleted + " 条",
                  desc: "会话已刷新",
                  undoId: r.undo_id || "",
                },
              });
            } else {
              toast("删除失败: " + errText(r, "未知错误"), "error");
            }
          });
        },
      }),
    };
  }

  function withdrawButton() {
    return {
      id: GROUP + ".regen",
      render: makeButton({
        title: "撤回本轮并编辑：删除本轮提问和回复，原文放回输入框，改完自己发送",
        confirm: "撤回本轮对话？原文会放进输入框。",
        icon: "UndoOutlined",
        fallbackEmoji: "↩",
        action: function (args, done) {
          var sid = getSessionId();
          if (!sid) { toast("无法获取会话 ID", "error"); done(); return; }
          var p = { created_at: args.locator.created_at, original_id: args.locator.original_id,
                    message_text: args.locator.message_text, role: args.locator.role };
          callApi("POST", apiPathFor(sid, "/regen"), p).then(function (r) {
            done();
            if (r && r.success) {
              scheduleReload({
                sessionId: sid,
                text: r.rewound_message || "",
                notify: {
                  title: "↩ 已撤回本轮",
                  desc: "原提问已放回输入框，改好后手动发送；点撤销可找回原对话",
                  undoId: r.undo_id || "",
                },
              });
            } else {
              toast("撤回失败: " + errText(r, "未知错误"), "error");
            }
          });
        },
      }),
    };
  }

  // ── 调试按钮（DEBUG 开关控制） ───────────────────────────

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  function dumpPage(title, htmlBody) {
    try {
      var win = window.open("", "_blank");
      if (win) {
        win.document.write('<!DOCTYPE html><html><head><meta charset="utf-8"><title>' +
          escapeHtml(title) + "</title><style>" +
          "body{background:#1a1a2e;color:#e0e0e0;font-family:Consolas,monospace;font-size:13px;line-height:1.6;padding:24px;margin:0}" +
          "pre{white-space:pre-wrap;word-break:break-all;margin:0}h2{color:#ffd700;margin-top:0}</style></head><body>" +
          "<h2>" + escapeHtml(title) + "</h2><hr style='border-color:#444'>" + htmlBody + "</body></html>");
        win.document.close();
        return true;
      }
    } catch (_) {}
    return false;
  }

  function inspectButton() {
    return {
      id: GROUP + ".inspect",
      render: makeButton({
        title: "查看该消息在会话文件中的完整 JSON",
        icon: "FileSearchOutlined",
        fallbackEmoji: "📄",
        alwaysOn: true,
        action: function (args, done) {
          var sid = getSessionId();
          if (!sid) { toast("无法获取会话 ID", "error"); done(); return; }
          var p = { created_at: args.locator.created_at, original_id: args.locator.original_id,
                    message_text: args.locator.message_text, role: args.locator.role };
          callApi("GET", apiPathFor(sid, "/message"), p).then(function (r) {
            done();
            if (r && r.success) {
              var s = JSON.stringify(r.message, null, 2);
              if (!dumpPage("📄 消息 JSON (index " + r.index + ")", "<pre>" + escapeHtml(s) + "</pre>")) {
                console.log("[" + GROUP + "] message JSON:", s);
                toast("JSON 已打印到控制台", "info");
              }
            } else {
              toast("获取失败: " + errText(r, "未知错误"), "error");
            }
          });
        },
      }),
    };
  }

  function deepProbe(data) {
    var result = {};
    var seen = new WeakSet();
    (function walk(obj, prefix) {
      if (!obj || typeof obj !== "object") { result[prefix] = String(obj); return; }
      if (seen.has(obj)) { result[prefix] = "[Circular]"; return; }
      seen.add(obj);
      Object.keys(obj).forEach(function (k) {
        var v = obj[k], full = prefix ? prefix + "." + k : k;
        if (v === null) result[full] = "null";
        else if (Array.isArray(v)) {
          result[full] = "Array[" + v.length + "]";
          if (v.length > 0) walk(v[0], full + "[0]");
        } else if (typeof v === "object") { result[full] = "object"; walk(v, full); }
        else result[full] = String(v);
      });
    })(data, "");
    return result;
  }

  function debugButton() {
    return {
      id: GROUP + ".debug",
      render: makeButton({
        title: "探测 ctx.data",
        icon: "BugOutlined",
        fallbackEmoji: "🔍",
        alwaysOn: true,
        action: function (args, done) {
          var flat = deepProbe(args.data || {});
          var lines = Object.keys(flat).map(function (k) {
            return (k || "(data)") + " => " + flat[k];
          });
          done();
          if (!dumpPage("🔍 ctx.data 探测", "<pre>" + escapeHtml(lines.join("\n")) + "</pre>")) {
            console.log("[" + GROUP + "] ctx.data:", flat);
            toast("探测结果已打印到控制台", "info");
          }
        },
      }),
    };
  }

  // ── 注册 ─────────────────────────────────────────────────

  function registerButtons() {
    var chat = window.QwenPaw && window.QwenPaw.chat;
    if (!chat || !chat.actions || !chat.requestActions) {
      console.warn("[" + GROUP + "] chat API not ready, retrying...");
      setTimeout(registerButtons, 500);
      return;
    }
    if (!host().React) {
      console.warn("[" + GROUP + "] host.React not ready, retrying...");
      setTimeout(registerButtons, 500);
      return;
    }

    // 刷新后还原待处理状态（回填/撤销通知/跳转 fork）
    restorePending(consumePending());

    try {
      // 用户气泡：↩撤回 删除 ⏪回退 🍴分叉（顺序与助手气泡一致，宿主按注册顺序渲染）
      chat.requestActions.add(GROUP, withdrawButton());
      chat.requestActions.add(GROUP, deleteButton());
      chat.requestActions.add(GROUP, rewindButton());
      chat.requestActions.add(GROUP, forkButton());
      // 助手气泡：↩撤回并编辑 🗑删除本轮 ⏪ 🍴
      chat.actions.add(GROUP, withdrawButton());
      chat.actions.add(GROUP, deleteButton());
      chat.actions.add(GROUP, rewindButton());
      chat.actions.add(GROUP, forkButton());
      if (DEBUG) {
        var dbg = [debugButton(), inspectButton()];
        dbg.forEach(function (b) { chat.requestActions.add(GROUP, b); });
        dbg.forEach(function (b) { chat.actions.add(GROUP, b); });
      }
      console.log("[" + GROUP + "] 按钮注册完成" + (DEBUG ? " (debug)" : ""));
    } catch (e) {
      console.error("[" + GROUP + "] 注册按钮失败:", e);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", registerButtons);
  } else {
    registerButtons();
  }
})();
