# -*- coding: utf-8 -*-
"""
生成「启动器网页端.html」：把界面单独导出成一份能用浏览器直接打开的网页。

用法：
    python build_web.py

产物：上级目录的 启动器网页端.html
打开后会自动在本机 18920~18975 端口里找已经跑起来的脆冰启动器服务，
找到了就直接用（和窗口里的界面一模一样），没找到会给提示。
也可以手动指定地址： 启动器网页端.html 打开后追加 ?base=http://127.0.0.1:18920
"""
from __future__ import annotations

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE_DIR, "ui", "index.html")
OUT = os.path.join(os.path.dirname(BASE_DIR), "启动器网页端.html")

SHIM = """<script>
/* ---- 网页端引导：自动找到本机的脆冰启动器服务 ---- */
(function () {
  var KEY = "kcl.web.base";
  function norm(u) { return String(u || "").replace(/\\/+$/, ""); }
  function probe(u) {
    try {
      var x = new XMLHttpRequest();
      x.open("GET", u + "/api/hello", false);
      x.timeout = 1500;
      x.send();
      if (x.status === 200) {
        var j = JSON.parse(x.responseText);
        return !!(j && j.ok);
      }
    } catch (e) {}
    return false;
  }
  var q = new URLSearchParams(location.search);
  var base = norm(q.get("base") || "");
  if (!base) { try { base = norm(localStorage.getItem(KEY) || ""); } catch (e) {} }
  if (base && !probe(base)) base = "";
  if (!base) {
    for (var p = 18920; p <= 18975; p++) {
      var u = "http://127.0.0.1:" + p;
      if (probe(u)) { base = u; break; }
    }
  }
  if (base) { try { localStorage.setItem(KEY, base); } catch (e) {} }
  window.__KCL_BASE__ = base;

  var _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    try {
      if (typeof input === "string" && input.charAt(0) === "/" && window.__KCL_BASE__) {
        input = window.__KCL_BASE__ + input;
      }
    } catch (e) {}
    return _fetch(input, init);
  };
  var _open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, url) {
    try {
      if (typeof url === "string" && url.charAt(0) === "/" && window.__KCL_BASE__) {
        arguments[1] = window.__KCL_BASE__ + url;
      }
    } catch (e) {}
    return _open.apply(this, arguments);
  };

  function bar(text, bad, retry) {
    var el = document.getElementById("kclWebBar");
    if (!el) return;
    el.textContent = text;
    el.style.background = bad ? "#b3403a" : "#1f6f8b";
    var b = document.getElementById("kclWebRetry");
    if (b) b.style.display = retry ? "" : "none";
  }
  window.addEventListener("DOMContentLoaded", function () {
    var el = document.createElement("div");
    el.id = "kclWebBar";
    el.style.cssText = "position:fixed;left:0;right:0;top:0;z-index:99999;" +
      "color:#fff;font:13px/1.6 'Microsoft YaHei',system-ui,sans-serif;" +
      "padding:6px 12px;display:flex;gap:10px;align-items:center;background:#1f6f8b";
    el.innerHTML = '<span id="kclWebTxt"></span>' +
      '<button id="kclWebRetry" style="margin-left:auto;cursor:pointer;border:1px solid ' +
      '#fff;background:transparent;color:#fff;border-radius:8px;padding:2px 10px;' +
      'font:inherit">重新连接</button>' +
      '<button id="kclWebSet" style="cursor:pointer;border:1px solid #fff;' +
      'background:transparent;color:#fff;border-radius:8px;padding:2px 10px;' +
      'font:inherit">手动填地址</button>';
    document.body.appendChild(el);
    document.body.style.paddingTop = "34px";
    var txt = el.querySelector("#kclWebTxt");
    function paint() {
      if (window.__KCL_BASE__) {
        txt.textContent = "网页端 · 已连接本机启动器 " + window.__KCL_BASE__;
        el.style.background = "#1f6f8b";
        el.querySelector("#kclWebRetry").style.display = "none";
      } else {
        txt.textContent = "网页端 · 没找到本机启动器：请先双击启动器 exe，或手动填服务地址";
        el.style.background = "#b3403a";
        el.querySelector("#kclWebRetry").style.display = "";
      }
    }
    paint();
    el.querySelector("#kclWebRetry").onclick = function () { location.reload(); };
    el.querySelector("#kclWebSet").onclick = function () {
      var v = prompt("本机启动器地址，例如 http://127.0.0.1:18920",
                     window.__KCL_BASE__ || "http://127.0.0.1:18920");
      if (!v) return;
      v = norm(v);
      try { localStorage.setItem(KEY, v); } catch (e) {}
      location.href = location.pathname + "?base=" + encodeURIComponent(v);
    };
  });
})();
</script>
"""


def build() -> str:
    with open(SRC, "r", encoding="utf-8") as f:
        html = f.read()
    if "__KCL_BASE__" in html:
        html = html.replace("__KCL_BASE__", "KCLBASE")   # 防重复注入
    head = html.find("<head>")
    if head < 0:
        raise RuntimeError("界面文件里找不到 <head>")
    pos = head + len("<head>")
    html = html[:pos] + "\n" + SHIM + html[pos:]
    html = html.replace("<title>", "<!-- web end --><title>", 1)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("[build] 已生成：%s（%.1f KB）" % (OUT, os.path.getsize(OUT) / 1024))
    return OUT


if __name__ == "__main__":
    build()
    sys.exit(0)
