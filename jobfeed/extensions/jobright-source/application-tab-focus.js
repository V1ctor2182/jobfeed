(function (root) {
  const STORAGE_KEY = "applicationTabFocus";
  const DEFAULT_URL = "https://jobfeed.localhost:1355";

  function dashboardBase(value) {
    const url = new URL(value);
    const local = url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname);
    if ((!local && url.origin !== DEFAULT_URL) || url.username || url.password
      || url.pathname !== "/" || url.search || value.includes("#")) throw new Error("Use the local Jobfeed dashboard address.");
    return url.origin;
  }

  function pageIdentity(value) {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("Open an application page first.");
    return url.href.split("#", 1)[0];
  }

  function create(api) {
    const { chrome, WebSocket, fetch, crypto, AbortController, setTimeout, clearTimeout, setInterval, clearInterval } = api;
    const connections = new Map();
    const bridgeRequests = new Map();
    let saved;
    const loaded = chrome.storage.session.get(STORAGE_KEY).then(result => {
      saved = result[STORAGE_KEY] || { browser_session_id: crypto.randomUUID(), bindings: [] };
      return chrome.storage.session.set({ [STORAGE_KEY]: saved });
    });
    const persist = () => chrome.storage.session.set({ [STORAGE_KEY]: saved });

    function stop(bindingId) {
      const state = connections.get(bindingId);
      if (!state) return;
      connections.delete(bindingId);
      clearTimeout(state.retry);
      clearInterval(state.heartbeat);
      clearTimeout(state.deadline);
      state.reject?.(new Error("Tab connection ended. Connect this tab again."));
      state.socket?.close();
    }

    async function forget(bindingId) {
      await loaded;
      stop(bindingId);
      saved.bindings = saved.bindings.filter(item => item.binding_id !== bindingId);
      await persist();
    }

    async function focus(binding, expiresAt, connected = () => true) {
      const expired = () => !Number.isFinite(expiresAt) || expiresAt < Date.now();
      const permitted = () => !expired() && connected() && saved.bindings.includes(binding);
      if (!permitted()) return "focus_failed";
      let tab;
      try { tab = await chrome.tabs.get(binding.native_tab_id); } catch { return "tab_closed"; }
      try {
        if (pageIdentity(tab.url) !== binding.url) return "page_changed";
        if (!permitted()) return "focus_failed";
        await chrome.tabs.update(tab.id, { active: true });
        if (!permitted()) return "focus_failed";
        await chrome.windows.update(tab.windowId, { focused: true });
        const current = await chrome.tabs.get(tab.id);
        const window = await chrome.windows.get(current.windowId);
        if (pageIdentity(current.url) !== binding.url) return "page_changed";
        return current.active && window.focused ? "focused" : "focus_failed";
      } catch { return "focus_failed"; }
    }

    function connectSocket(binding) {
      stop(binding.binding_id);
      const state = { socket: null, retry: null, heartbeat: null, deadline: null, reject: null, ready: false };
      connections.set(binding.binding_id, state);
      return new Promise((resolve, reject) => {
        state.reject = reject;
        const socket = new WebSocket(`${binding.base_url.replace(/^http/, "ws")}/api/application-tabs/bridge`);
        state.socket = socket;
        state.deadline = setTimeout(() => {
          state.reject?.(new Error("The dashboard could not connect to this tab. Try connecting again."));
          state.reject = null;
          socket.close();
        }, 5000);
        socket.addEventListener("open", () => {
          if (connections.get(binding.binding_id) !== state) return;
          socket.send(JSON.stringify({ type: "hello", binding_id: binding.binding_id,
            session_token: binding.session_token, browser_session_id: saved.browser_session_id }));
        });
        socket.addEventListener("message", event => {
          if (connections.get(binding.binding_id) !== state) return;
          let message;
          try { message = JSON.parse(event.data); } catch { return; }
          if (message.type === "ready") {
            state.ready = true;
            clearTimeout(state.deadline);
            state.reject = null;
            clearInterval(state.heartbeat);
            state.heartbeat = setInterval(() => {
              if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ping" }));
            }, 20000);
            resolve();
          } else if (message.type === "error") {
            void forget(binding.binding_id);
          } else if (message.type === "focus" && message.binding_id === binding.binding_id
            && typeof message.request_id === "string" && message.request_id.length <= 200) {
            void focus(binding, message.expires_at, () => state.ready
              && connections.get(binding.binding_id) === state && socket.readyState === WebSocket.OPEN).then(status => {
              if (connections.get(binding.binding_id) === state && socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({ type: "focus_result", request_id: message.request_id,
                  binding_id: binding.binding_id, status }));
              }
              if (status === "tab_closed") void forget(binding.binding_id);
            });
          }
        });
        socket.addEventListener("close", event => {
          if (connections.get(binding.binding_id) !== state) return;
          state.ready = false;
          clearTimeout(state.deadline);
          clearInterval(state.heartbeat);
          state.reject?.(new Error("The tab connection was refused. Create a new code and connect again."));
          state.reject = null;
          if (event.code === 1008 || event.code >= 4000) {
            void forget(binding.binding_id);
            return;
          }
          state.retry = setTimeout(() => {
            if (connections.get(binding.binding_id) === state && saved.bindings.includes(binding)) {
              void connectSocket(binding).catch(() => {});
            }
          }, 5000);
        });
      });
    }

    async function connectTab(baseValue, codeValue, target) {
      await loaded;
      const base_url = dashboardBase(baseValue);
      const code = String(codeValue || "").trim();
      if (!code || code.length > 200) throw new Error("Enter the pairing code from this job in Runs.");
      const tab = target ? await chrome.tabs.get(target.native_tab_id)
        : (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
      if (!tab || !Number.isInteger(tab.id) || tab.id < 0) throw new Error("Open the existing application tab first.");
      if (target && (tab.id !== target.native_tab_id || tab.url !== target.url)) throw new Error("The recorded application tab changed URL or belongs to another profile.");
      const url = pageIdentity(tab.url);
      let response, result;
      const abort = new AbortController();
      const deadline = setTimeout(() => abort.abort(), 8000);
      try {
        response = await fetch(`${base_url}/api/application-tabs/connect`, {
          method: "POST", headers: { "Content-Type": "application/json" }, signal: abort.signal,
          body: JSON.stringify({ code, browser_session_id: saved.browser_session_id, native_tab_id: tab.id, url }),
        });
        if (response.ok) result = await response.json();
      } catch { throw new Error("Cannot reach the local dashboard. Check its address and try again."); }
      finally { clearTimeout(deadline); }
      if (!response.ok) throw new Error("This code could not connect the tab. Check the selected job and create a new code.");
      if (typeof result?.binding_id !== "string" || !result.binding_id || typeof result.session_token !== "string" || !result.session_token) {
        throw new Error("The dashboard returned an invalid connection. Create a new code.");
      }
      for (const prior of [...saved.bindings]) if (prior.native_tab_id === tab.id) await forget(prior.binding_id);
      const binding = { binding_id: result.binding_id, session_token: result.session_token,
        native_tab_id: tab.id, url, base_url };
      saved.bindings.push(binding);
      await persist();
      await connectSocket(binding);
      return { connected: true, binding_id: binding.binding_id };
    }

    async function bridgeCommand(command, connected = () => true) {
      await loaded;
      if (typeof command.request_id !== "string" || !command.request_id || command.request_id.length > 100
        || !Number.isFinite(command.expires_at) || command.expires_at < Date.now() || !connected()) throw new Error("Application tab request expired or disconnected.");
      if (bridgeRequests.has(command.request_id)) return bridgeRequests.get(command.request_id);
      if (bridgeRequests.size >= 100) bridgeRequests.delete(bridgeRequests.keys().next().value);
      const pending = (async () => {
        if (command.action === "probe") {
          if (!Array.isArray(command.candidates) || !command.candidates.length || command.candidates.length > 16) throw new Error("Invalid profile probe.");
          const matches = [];
          for (const candidate of command.candidates) {
            if (!Number.isInteger(candidate.native_tab_id) || candidate.native_tab_id <= 0 || typeof candidate.url !== "string" || candidate.url.length > 4096) throw new Error("Invalid profile candidate.");
            pageIdentity(candidate.url);
            try {
              const tab = await chrome.tabs.get(candidate.native_tab_id);
              if (tab.id === candidate.native_tab_id && tab.url === candidate.url) matches.push(candidate);
            } catch { /* A different profile cannot access this tab. */ }
          }
          if (!connected() || command.expires_at < Date.now()) throw new Error("Application tab request expired or disconnected.");
          return { browser_session_id: saved.browser_session_id, matches };
        }
        if (command.action === "connect") {
          if (command.browser_session_id !== saved.browser_session_id) throw new Error("Jobfeed Chrome profile session changed.");
          if (!Number.isInteger(command.native_tab_id) || command.native_tab_id <= 0 || typeof command.url !== "string") throw new Error("Invalid application tab.");
          const result = await connectTab(command.base_url, command.code, command);
          if (!connected() || command.expires_at < Date.now()) throw new Error("Application tab connection outcome is unconfirmed.");
          return result;
        }
        throw new Error("Unsupported application tab command.");
      })();
      bridgeRequests.set(command.request_id, pending);
      return pending;
    }

    async function removeTab(tabId) {
      await loaded;
      for (const binding of [...saved.bindings]) if (binding.native_tab_id === tabId) await forget(binding.binding_id);
    }

    async function restore() {
      await loaded;
      for (const binding of [...saved.bindings]) {
        try {
          dashboardBase(binding.base_url);
          const tab = await chrome.tabs.get(binding.native_tab_id);
          if (pageIdentity(tab.url) !== binding.url) throw new Error("Page changed");
          void connectSocket(binding).catch(() => {});
        } catch { await forget(binding.binding_id); }
      }
    }

    chrome.tabs.onRemoved.addListener(tabId => { void removeTab(tabId); });
    chrome.tabs.onReplaced.addListener((_added, removed) => { void removeTab(removed); });
    chrome.runtime.onMessage.addListener((message, sender, respond) => {
      if (message?.type !== "application_tab_connect") return false;
      if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("popup.html")) return false;
      void connectTab(message.baseUrl, message.code).then(respond, error => respond({ error: error.message }));
      return true;
    });
    return { connectTab, restore, focus, removeTab, bridgeCommand };
  }

  root.ApplicationTabFocus = { create, dashboardBase, pageIdentity, DEFAULT_URL };
  if (typeof chrome !== "undefined") {
    const controller = create({ chrome, WebSocket, fetch, crypto, AbortController, setTimeout, clearTimeout, setInterval, clearInterval });
    root.ApplicationTabFocus.controller = controller;
    void controller.restore().catch(() => {});
  }
})(globalThis);
