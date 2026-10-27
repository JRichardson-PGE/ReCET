// Docs pages: draw the ```mermaid blocks of a document (Python-Markdown writes them as <pre><code class="language-mermaid">) with the vendored Mermaid.
// Loaded only on a page that has one (app.py, doc). The page's CSP allows only same-origin scripts, so Mermaid is served from static/vendor/mermaid.
(function () {
  if (typeof mermaid === "undefined") return;
  var blocks = document.querySelectorAll("article.doc pre > code.language-mermaid");
  if (!blocks.length) return;
  blocks.forEach(function (code) {
    var div = document.createElement("div");
    div.className = "mermaid";
    div.textContent = code.textContent;            // the diagram's source, unescaped
    code.parentNode.replaceWith(div);
  });
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  mermaid.initialize({startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "default"});
  mermaid.run({querySelector: "article.doc .mermaid"});
})();
