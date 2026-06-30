/**
 * trace-capture.js
 * ------------------
 * 在 index.html 里加一行引入即可,不需要改动任何现有业务代码:
 *
 *     <script src="/trace-capture.js"></script>
 *
 * 自动捕获:
 *   - 点击事件(点了什么元素、文字/id/data-testid/aria-label)
 *   - console.error 输出
 *   - 未捕获的异常 / Promise rejection
 *   - fetch 请求返回非 2xx,或请求本身失败
 *
 * 所有事件统一 POST 到后端的 /__trace 端点,由 trace_endpoint.py 落盘成
 * trace.jsonl。这一层只负责记录,不做任何理解或总结。
 *
 * 注意:如果你的 SSE 流式响应是用 fetch + ReadableStream 手动读的(而不是
 * 原生 EventSource),下面的 fetch 拦截只会捕获"发起请求"那一刻的失败,
 * 不包含流读取到一半时中途报错的情况。如果需要连这部分也捕获,在你自己
 * 读 stream 的那个循环里,catch 到异常时调用一下 window.__trace('stream_error', {...})
 * 即可(本文件已经把这个函数挂在 window 上,方便你直接调用)。
 */

(function () {
  const ENDPOINT = "/__trace";

  function send(type, payload) {
    try {
      fetch(ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type, ...payload }),
        keepalive: true,
      }).catch(() => {});
    } catch (_) {
      // 静默失败,不影响正常业务逻辑
    }
  }

  // 暴露出去,方便手动上报(比如上面提到的 SSE 流中途报错的情况)
  window.__trace = send;

  // 点击事件(capture 阶段,确保即使元素后续阻止冒泡也能捕获到)
  document.addEventListener(
    "click",
    (e) => {
      const el = e.target;
      if (!el || !el.tagName) return;
      send("click", {
        tag: el.tagName,
        text: (el.textContent || "").slice(0, 80).trim(),
        id: el.id || null,
        testId: el.getAttribute("data-testid"),
        ariaLabel: el.getAttribute("aria-label"),
      });
    },
    true
  );

  // console.error 拦截
  const origError = console.error;
  console.error = function (...args) {
    send("console_error", { text: args.map(String).join(" ") });
    origError.apply(console, args);
  };

  // 未捕获异常
  window.addEventListener("error", (e) => {
    send("uncaught_error", {
      message: e.message,
      source: e.filename,
      line: e.lineno,
    });
  });
  window.addEventListener("unhandledrejection", (e) => {
    send("unhandled_rejection", { reason: String(e.reason) });
  });

  // fetch 失败 / 非 2xx 响应(包含调用你 FastAPI 后端接口失败的情况)
  const origFetch = window.fetch;
  window.fetch = function (...args) {
    return origFetch
      .apply(this, args)
      .then((res) => {
        if (!res.ok) {
          send("http_error", { url: String(args[0]), status: res.status });
        }
        return res;
      })
      .catch((err) => {
        send("fetch_failed", { url: String(args[0]), message: String(err) });
        throw err;
      });
  };
})();
