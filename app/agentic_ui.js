/* Agent chat client.
 *
 * Subsystems: Icons (inline SVG set), Markdown (safe rendering), Syntax
 * (markdown highlighting), Media (inline preview), Clipboard (copy with an
 * http fallback), Blocks (content subscribers, updated concurrently and out of
 * order), Theme (VS Code theme subset), Commands (slash commands), Questions
 * (what the model asked), Feedback (votes that teach rules), Resume (an
 * unfinished run), Wait (a silent model's timer), Saved (the last session),
 * Draft (the unsent text), Editor (the composer lifted into a markdown
 * editor) and Socket (hub client).
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = {
    app: $("app"),
    brand: $("brand"),
    wait: $("wait"),
    status: $("status-text"),
    session: $("session"),
    gauge: $("gauge"),
    markPlan: $("mark-plan"),
    plan: $("plan"),
    planCount: $("plan-count"),
    planNow: $("plan-now"),
    planPanel: $("plan-panel"),
    planList: $("plan-list"),
    planFoot: $("plan-foot"),
    discussion: $("discussion"),
    empty: $("empty"),
    form: $("form"),
    input: $("input"),
    send: $("send"),
    stop: $("stop"),
    file: $("file"),
    attachments: $("attachments"),
    hints: $("hints"),
    lift: $("lift"),
    drop: $("drop"),
    editor: $("editor"),
    editorText: $("editor-text"),
    editorPaint: $("editor-paint"),
    editorCopy: $("editor-copy"),
  };

  // ------------------------------------------------------------------ icons

  /** A small stroke icon set, inline with the code: no font, no network. */
  const Icons = {
    paths: {
      "chevron-down": '<path d="m6 9 6 6 6-6"/>',
      copy:
        '<rect x="9" y="9" width="13" height="13" rx="2"/>' +
        '<path d="M5 15a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2"/>',
      check: '<path d="M20 6 9 17l-5-5"/>',
      close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
      user:
        '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/>' +
        '<circle cx="12" cy="7" r="4"/>',
      bot:
        '<path d="M12 8V4H8"/><rect x="4" y="8" width="16" height="12" rx="2"/>' +
        '<path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
      sparkles:
        '<path d="m12 3 1.9 4.6 4.6 1.9-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9Z"/>' +
        '<path d="m18 15.4.8 1.8 1.8.8-1.8.8-.8 1.8-.8-1.8-1.8-.8 1.8-.8Z"/>',
      wrench:
        '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-8 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-8l-3.7 3.9z"/>',
      info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
      help:
        '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/>' +
        '<path d="M12 17h.01"/>',
      alert:
        '<path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z"/>' +
        '<path d="M12 9v4"/><path d="M12 17h.01"/>',
      "thumbs-up":
        '<path d="M7 10v12"/><path d="M15 5.9 14 10h5.8a2 2 0 0 1 1.9 2.6l-2.3 8A2 2 0 0 1 17.5 22H4' +
        'a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.8a2 2 0 0 0 1.8-1.1L12 2a3.1 3.1 0 0 1 3 3.9Z"/>',
      "thumbs-down":
        '<path d="M17 14V2"/><path d="M9 18.1 10 14H4.2a2 2 0 0 1-1.9-2.6l2.3-8A2 2 0 0 1 6.5 2H20' +
        'a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.8a2 2 0 0 0-1.8 1.1L12 22a3.1 3.1 0 0 1-3-3.9Z"/>',
      play: '<path d="M6 3.5v17a1 1 0 0 0 1.5.9l13-8.5a1 1 0 0 0 0-1.8l-13-8.5A1 1 0 0 0 6 3.5Z"/>',
      shrink:
        '<path d="m14 10 7-7"/><path d="M20 10h-6V4"/><path d="m3 21 7-7"/><path d="M4 14h6v6"/>',
      circle: '<circle cx="12" cy="12" r="9"/>',
      "circle-dot": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>',
      "circle-check": '<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
    },
    /** Icon markup for a static template. The set is ours, so it is safe HTML. */
    markup(name, className) {
      const body = this.paths[name] || this.paths.info;
      return (
        `<svg class="${className || "icon"}" viewBox="0 0 24 24" ` +
        `aria-hidden="true" focusable="false">${body}</svg>`
      );
    },
  };

  /** The icon that stands for each kind of block. */
  const KIND_ICONS = {
    prompt: "user",
    output: "bot",
    reasoning: "sparkles",
    tool: "wrench",
    question: "help",
    log: "info",
    warning: "alert",
    error: "alert",
    compact: "shrink",
  };

  // ---------------------------------------------------------------- markdown

  const Markdown = (() => {
    const escape = (text) =>
      text.replace(
        /[&<>"']/g,
        (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
      );

    const safeUrl = (raw) => {
      const url = (raw || "").trim();
      if (/^(https?:|mailto:|\/|\.{0,2}\/)/i.test(url)) return url;
      if (/^data:(image|video|audio)\//i.test(url)) return url;
      return "";
    };

    //: Stands in for a code span while the rest of the inline markup is applied.
    const SPAN = "\u0000";

    /** Inline markup. Code spans are lifted out first so nothing rewrites them. */
    const inline = (text) => {
      const spans = [];
      const lifted = String(text).replace(/(`+)([\s\S]*?)\1/g, (_m, _t, code) => {
        spans.push(code.replace(/^ | $/g, ""));
        return `${SPAN}${spans.length - 1}${SPAN}`;
      });
      const html = escape(lifted)
        .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) => Media.tag(url, alt) || m)
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
          const href = safeUrl(url);
          return href
            ? `<a href="${escape(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`
            : m;
        })
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/__([^_]+)__/g, "<strong>$1</strong>")
        .replace(/~~([^~]+)~~/g, "<del>$1</del>")
        .replace(/(^|\W)\*([^*]+)\*/g, "$1<em>$2</em>")
        .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, (m, pre, url) => {
          const media = Media.tag(url, "");
          return media
            ? pre + media
            : `${pre}<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(url)}</a>`;
        });
      return html.replace(
        /\u0000(\d+)\u0000/g,
        (_m, index) => `<code>${escape(spans[Number(index)])}</code>`,
      );
    };

    const ALIGNMENTS = { ":-": "left", "-:": "right", "::": "center" };

    /** The alignment row of a pipe table, or null when the line is not one. */
    const alignments = (line) => {
      if (!/\|/.test(line) || !/^[\s|:-]+$/.test(line)) return null;
      const cells = split(line);
      if (!cells.length || !cells.every((cell) => /^:?-{1,}:?$/.test(cell.trim())))
        return null;
      return cells.map((cell) => {
        const text = cell.trim();
        const edges = `${text.startsWith(":") ? ":" : "-"}${text.endsWith(":") ? ":" : "-"}`;
        return ALIGNMENTS[edges] || "";
      });
    };

    /** The cells of one table row, without the outer pipes. */
    const split = (line) =>
      line
        .trim()
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split(/(?<!\\)\|/)
        .map((cell) => cell.replace(/\\\|/g, "|"));

    const cell = (tag, text, align) =>
      `<${tag}${align ? ` style="text-align:${align}"` : ""}>${inline(text.trim())}</${tag}>`;

    /** Render a markdown subset to HTML. Input is escaped before anything else. */
    const render = (source) => {
      const lines = String(source || "").split("\n");
      const out = [];
      const lists = [];
      let paragraph = [];

      const flushParagraph = () => {
        if (paragraph.length) {
          out.push(`<p>${inline(paragraph.join("\n"))}</p>`);
          paragraph = [];
        }
      };
      const closeLists = (indent) => {
        while (lists.length && (indent === undefined || indent < lists[lists.length - 1].indent)) {
          const list = lists.pop();
          if (list.item) out.push("</li>");
          out.push(`</${list.tag}>`);
        }
      };
      const flush = () => {
        flushParagraph();
        closeLists();
      };
      const openItem = (indent, tag, text) => {
        flushParagraph();
        closeLists(indent);
        const top = lists[lists.length - 1];
        if (!top || indent > top.indent) {
          out.push(`<${tag}>`);
          lists.push({ tag, indent, item: false });
        } else if (top.tag !== tag) {
          if (top.item) out.push("</li>");
          out.push(`</${top.tag}>`);
          lists.pop();
          out.push(`<${tag}>`);
          lists.push({ tag, indent, item: false });
        }
        const list = lists[lists.length - 1];
        if (list.item) out.push("</li>");
        const task = /^\[([ xX])\]\s+(.*)$/.exec(text);
        if (task) {
          const checked = task[1].toLowerCase() === "x" ? " checked" : "";
          out.push(
            `<li class="task"><input type="checkbox" disabled${checked} /> ${inline(task[2])}`,
          );
        } else {
          out.push(`<li>${inline(text)}`);
        }
        list.item = true;
      };

      for (let i = 0; i < lines.length; i += 1) {
        const line = lines[i].replace(/\t/g, "    ");

        const fence = /^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$/.exec(line);
        if (fence) {
          flush();
          const body = [];
          const marker = fence[1][0];
          i += 1;
          for (; i < lines.length; i += 1) {
            if (new RegExp(`^\\s*${marker}{3,}\\s*$`).test(lines[i])) break;
            body.push(lines[i]);
          }
          const language = fence[2]
            ? ` class="language-${escape(fence[2].toLowerCase())}"`
            : "";
          out.push(`<pre><code${language}>${escape(body.join("\n"))}</code></pre>`);
          continue;
        }

        if (!line.trim()) {
          flush();
          continue;
        }

        const heading = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
        if (heading) {
          flush();
          const level = heading[1].length;
          out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
          continue;
        }

        if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
          flush();
          out.push("<hr />");
          continue;
        }

        const quote = /^\s*>\s?(.*)$/.exec(line);
        if (quote) {
          flush();
          const body = [quote[1]];
          while (i + 1 < lines.length && /^\s*>\s?/.test(lines[i + 1])) {
            body.push(lines[(i += 1)].replace(/^\s*>\s?/, ""));
          }
          out.push(`<blockquote>${inline(body.join("\n"))}</blockquote>`);
          continue;
        }

        // A pipe table: a header row, an alignment row, then the body.
        const columns = i + 1 < lines.length ? alignments(lines[i + 1]) : null;
        if (columns && /\|/.test(line)) {
          flush();
          const head = split(line);
          const rows = [];
          i += 1;
          while (i + 1 < lines.length && /\|/.test(lines[i + 1]) && lines[i + 1].trim()) {
            rows.push(split(lines[(i += 1)]));
          }
          const header = head
            .map((text, index) => cell("th", text, columns[index]))
            .join("");
          const body = rows
            .map(
              (row) =>
                `<tr>${head
                  .map((_c, index) => cell("td", row[index] || "", columns[index]))
                  .join("")}</tr>`,
            )
            .join("");
          out.push(
            `<div class="table-wrap"><table><thead><tr>${header}</tr></thead>` +
            `<tbody>${body}</tbody></table></div>`,
          );
          continue;
        }

        const item = /^(\s*)(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line);
        if (item) {
          openItem(item[1].length, item[2] ? "ul" : "ol", item[4]);
          continue;
        }

        // An indented line under an open item continues that item.
        const list = lists[lists.length - 1];
        if (list && list.item && /^\s{2,}/.test(line)) {
          out.push(` ${inline(line.trim())}`);
          continue;
        }

        closeLists();
        paragraph.push(line);
      }
      flush();
      return out.join("\n");
    };

    return { render, escape, safeUrl, inline };
  })();

  // ----------------------------------------------------------------- syntax

  /** Markdown highlighting for the editor. Every character of the source comes
   *  out exactly once, escaped and in order, so the painted layer lines up
   *  glyph for glyph with the textarea over it; a span may only change colour. */
  const Syntax = (() => {
    const { escape } = Markdown;
    const span = (kind, html) => `<span class="md-${kind}">${html}</span>`;
    const mark = (text) => span("mark", escape(text));
    const wrap = (kind, edge, inner) => mark(edge) + span(kind, inline(inner)) + mark(edge);

    /** Inline rules, each tried from the same position: the earliest match
     *  wins, a tie goes to the rule listed first. */
    const RULES = [
      [/\\[\\`*_{}[\]()#+\-.!|~>]/g, (m) => mark(m[0])],
      [/(`+)([\s\S]*?)\1/g, (m) => span("code", mark(m[1]) + escape(m[2]) + mark(m[1]))],
      [
        /(!?\[)([^\]]*)(\]\()([^)\s]*)(\))/g,
        (m) => mark(m[1]) + span("link", inline(m[2])) + mark(m[3]) + span("url", escape(m[4])) + mark(m[5]),
      ],
      [/\*\*(?=\S)(.+?)(?<=\S)\*\*/g, (m) => wrap("strong", "**", m[1])],
      [/(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)/g, (m) => wrap("strong", "__", m[1])],
      [/\*(?=[^\s*])(.+?)(?<=[^\s*])\*/g, (m) => wrap("em", "*", m[1])],
      [/(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)/g, (m) => wrap("em", "_", m[1])],
      [/~~(?=\S)(.+?)(?<=\S)~~/g, (m) => wrap("del", "~~", m[1])],
      [/https?:\/\/[^\s<>()]+/g, (m) => span("url", escape(m[0]))],
    ];

    const inline = (text) => {
      let html = "";
      let at = 0;
      while (at < text.length) {
        let best = null;
        for (const [pattern, paint] of RULES) {
          pattern.lastIndex = at;
          const match = pattern.exec(text);
          if (match && (!best || match.index < best.match.index)) best = { match, paint };
        }
        if (!best) break;
        html += escape(text.slice(at, best.match.index)) + best.paint(best.match);
        at = best.match.index + best.match[0].length;
      }
      return html + escape(text.slice(at));
    };

    /** One line outside a fence: its block marker, then its inline markup. */
    const line = (text) => {
      let m = /^(\s{0,3})(#{1,6})(\s.*|)$/.exec(text);
      if (m) return escape(m[1]) + span("head", mark(m[2]) + inline(m[3]));
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(text)) return mark(text);
      m = /^(\s*(?:>\s?)+)(.*)$/.exec(text);
      if (m) return mark(m[1]) + span("quote", inline(m[2]));
      m = /^(\s*)([-*+]|\d{1,9}[.)])(\s+)(\[[ xX]\](?=\s|$))?(.*)$/.exec(text);
      if (m) {
        const task = m[4] ? span("list", escape(m[4])) : "";
        return escape(m[1]) + span("list", escape(m[2])) + escape(m[3]) + task + inline(m[5]);
      }
      // A pipe table: the alignment row is all punctuation, a row has cells.
      if (/^\s*\|/.test(text)) {
        if (/^[\s|:-]+$/.test(text)) return mark(text);
        return text
          .split(/(?<!\\)(\|)/)
          .map((piece) => (piece === "|" ? mark(piece) : inline(piece)))
          .join("");
      }
      return inline(text);
    };

    /** Highlight a whole document. Fences are tracked across lines so their
     *  bodies stay code, whatever they contain. */
    const highlight = (source) => {
      const out = [];
      let fence = null;
      String(source || "")
        .split("\n")
        .forEach((text) => {
          if (fence) {
            const closed = new RegExp(`^\\s*${fence[0]}{${fence.length},}\\s*$`).test(text);
            out.push(span(closed ? "fence" : "code", escape(text)));
            if (closed) fence = null;
            return;
          }
          const open = /^(\s*)(`{3,}|~{3,})(.*)$/.exec(text);
          if (open && !(open[2][0] === "`" && open[3].includes("`"))) {
            fence = open[2];
            out.push(escape(open[1]) + span("fence", escape(open[2])) + span("lang", escape(open[3])));
            return;
          }
          out.push(line(text));
        });
      return out.join("\n");
    };

    return { highlight, inline };
  })();

  // ------------------------------------------------------------------ media

  const Media = (() => {
    const KINDS = {
      image: /\.(png|jpe?g|gif|webp|bmp|svg)(\?|#|$)|^data:image\//i,
      video: /\.(mp4|webm|ogv|mov)(\?|#|$)|^data:video\//i,
      audio: /\.(mp3|wav|ogg|m4a|flac)(\?|#|$)|^data:audio\//i,
    };

    const kindOf = (url) =>
      Object.keys(KINDS).find((kind) => KINDS[kind].test(url)) || "";

    /** Return an inline media tag for a URL, or "" when it is not media. */
    const tag = (rawUrl, alt) => {
      const url = Markdown.safeUrl(rawUrl);
      if (!url) return "";
      const src = Markdown.escape(url);
      switch (kindOf(url)) {
        case "image":
          return `<img src="${src}" alt="${Markdown.escape(alt || "")}" loading="lazy" />`;
        case "video":
          return `<video src="${src}" controls playsinline preload="metadata"></video>`;
        case "audio":
          return `<audio src="${src}" controls preload="metadata"></audio>`;
        default:
          return "";
      }
    };

    return { tag, kindOf };
  })();

  // -------------------------------------------------------------- clipboard

  const Clipboard = {
    /** Copy text, falling back to a selection when the async API is barred. */
    async write(text) {
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          return true;
        }
      } catch {
        // the page is not allowed to use the async API; fall through
      }
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.cssText = "position:fixed;top:-1000px;opacity:0";
      document.body.appendChild(area);
      area.select();
      let copied = false;
      try {
        copied = document.execCommand("copy");
      } catch {
        copied = false;
      }
      area.remove();
      return copied;
    },
  };

  /** Show how a copy went on its button for a moment: a check or a cross. */
  const flash = (() => {
    const timers = new WeakMap();
    return (button, copied) => {
      button.innerHTML = Icons.markup(copied ? "check" : "close");
      button.title = copied ? "Copied" : "Copy failed";
      clearTimeout(timers.get(button));
      timers.set(
        button,
        setTimeout(() => {
          button.innerHTML = Icons.markup("copy");
          button.title = "Copy";
        }, 1400),
      );
    };
  })();

  // ------------------------------------------------------------------ theme

  /** CSS variable to the VS Code colors it is taken from, best first.
   *  Mirrors THEME_KEYS in main.py, which does the same for a server theme. */
  const THEME_KEYS = {
    "--bg": ["editor.background"],
    "--fg": ["editor.foreground", "foreground"],
    "--bg-soft": [
      "sideBar.background",
      "editorGroupHeader.tabsBackground",
      "activityBar.background",
    ],
    "--bg-raised": [
      "editorWidget.background",
      "dropdown.background",
      "menu.background",
      "input.background",
    ],
    "--input-bg": ["input.background", "editorWidget.background"],
    "--input-fg": ["input.foreground", "editor.foreground", "foreground"],
    "--input-border": [
      "input.border",
      "editorWidget.border",
      "panel.border",
      "contrastBorder",
    ],
    "--border": [
      "panel.border",
      "editorGroup.border",
      "editorWidget.border",
      "contrastBorder",
      "input.border",
    ],
    "--accent": ["button.background", "focusBorder"],
    "--accent-fg": ["button.foreground"],
    "--focus": ["focusBorder", "button.background"],
    "--muted": [
      "descriptionForeground",
      "editorLineNumber.foreground",
      "disabledForeground",
    ],
    "--error": ["errorForeground", "editorError.foreground", "inputValidation.errorBorder"],
    "--warn": ["editorWarning.foreground", "list.warningForeground", "inputValidation.warningBorder"],
    "--link": ["textLink.foreground", "textLink.activeForeground"],
    "--code-bg": ["textCodeBlock.background", "editorWidget.background"],
    "--badge-bg": ["badge.background"],
    "--badge-fg": ["badge.foreground"],
    "--scroll": ["scrollbarSlider.background"],
  };

  /** Variables worth nothing unless they differ from the page background: a
   *  code surface the color of the page is not a surface. Mirrors
   *  THEME_DISTINCT in main.py. */
  const THEME_DISTINCT = ["--code-bg"];

  const Theme = {
    /** Normalise the theme kinds VS Code writes onto the ones the CSS knows. */
    type(raw) {
      const value = String(raw || "").toLowerCase().replace(/[\s_]+/g, "-");
      if (value === "hc" || value === "hcdark" || value === "hc-dark") return "hc-dark";
      if (value === "hclight" || value === "hc-light") return "hc-light";
      return value === "light" ? "light" : "dark";
    },
    //: What the last theme set, so a new one can undo it. A theme that names
    //: fewer colors than the one before it must not inherit the difference.
    applied: [],
    /** Apply a CSS variable map (already mapped from a VS Code theme). */
    apply(vars) {
      if (!vars) return;
      const root = document.documentElement;
      this.applied.forEach((name) => root.style.removeProperty(name));
      this.applied = [];
      Object.entries(vars).forEach(([name, value]) => {
        if (name === "--theme-type") {
          root.dataset.themeType = this.type(value);
          return;
        }
        if (/^--[\w-]+$/.test(name) && typeof value === "string") {
          root.style.setProperty(name, value);
          this.applied.push(name);
        }
      });
    },
    /** Map a raw VS Code theme file onto CSS variables (client side loading). */
    fromVsCode(theme) {
      const colors = (theme && theme.colors) || {};
      const pick = (keys, unlike) => {
        const key = keys.find(
          (candidate) => colors[candidate] && String(colors[candidate]) !== unlike,
        );
        return key ? String(colors[key]) : null;
      };
      const background = pick(THEME_KEYS["--bg"]);
      const vars = {};
      Object.entries(THEME_KEYS).forEach(([name, keys]) => {
        const value = pick(keys, THEME_DISTINCT.includes(name) ? background : null);
        if (value !== null) vars[name] = value;
      });
      vars["--theme-type"] = this.type(theme && theme.type);
      return vars;
    },
  };

  // ----------------------------------------------------------------- blocks

  /** Report which edges of the discussion have content under them. A bar casts
   *  a shadow only while something is passing behind it, so a conversation
   *  that fits on the screen sits on a flat page. */
  const edges = () => {
    const view = el.discussion;
    const room = view.scrollHeight - view.clientHeight;
    el.app.dataset.underHead = String(view.scrollTop > 1);
    el.app.dataset.underFoot = String(room - view.scrollTop > 1);
  };

  /** A block subscribes to content and re-renders itself when it changes. A
   *  nested block is a worker's step, held by the call that started it. */
  class Block {
    constructor(id, kind, role, nested) {
      this.id = id;
      this.kind = kind || "output";
      this.nested = Boolean(nested);
      this.text = "";
      this.streaming = true;
      this.frame = 0;
      this.node = document.createElement("article");
      this.node.className = "block";
      this.node.dataset.kind = this.kind;
      this.node.dataset.streaming = "true";
      this.node.dataset.collapsed = "false";
      this.node.dataset.nested = String(this.nested);
      this.node.innerHTML =
        `<header class="block-head">` +
        `<button class="block-toggle" type="button" aria-expanded="true">` +
        Icons.markup("chevron-down", "icon chevron") +
        Icons.markup(KIND_ICONS[this.kind] || "bot", "icon kind") +
        `<span class="label"></span></button>` +
        `<span class="time"></span>` +
        `<span class="block-actions">` +
        `<button class="button tiny copy" type="button" title="Copy" ` +
        `aria-label="Copy to clipboard">${Icons.markup("copy")}</button>` +
        `</span></header><div class="block-body"></div>`;
      this.label(role === "user" ? "you" : this.kind);
      this.node.querySelector(".time").textContent = new Date().toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      });
      this.body = this.node.querySelector(".block-body");
      this.toggle = this.node.querySelector(".block-toggle");
      this.copyButton = this.node.querySelector(".copy");
      this.toggle.addEventListener("click", () => this.collapse());
      this.copyButton.addEventListener("click", () => this.copy());
      // a worker's answer is kept on its call, not as a turn: nothing to vote on
      if (this.kind === "output" && !this.nested) this.votes();
    }

    /** Hold a nested block: a worker's step under the call that started it. */
    nest(child) {
      if (!this.children) {
        this.children = document.createElement("div");
        this.children.className = "block-nest";
        this.body.after(this.children);
      }
      this.children.appendChild(child.node);
    }

    /** Thumbs on an answer: a vote teaches the agent a rule (see `/rules`). */
    votes() {
      this.voteButtons = {};
      [
        ["up", "thumbs-up", "Good answer"],
        ["down", "thumbs-down", "Bad answer"],
      ].forEach(([vote, icon, title]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button tiny vote";
        button.dataset.vote = vote;
        button.title = title;
        button.setAttribute("aria-label", title);
        button.setAttribute("aria-pressed", "false");
        button.innerHTML = Icons.markup(icon);
        button.addEventListener("click", () => Feedback.send(this, vote));
        this.copyButton.before(button);
        this.voteButtons[vote] = button;
      });
    }

    /** Show a vote: pending while its rule is learned, then pressed; none clears it. */
    voted(vote, pending) {
      this.node.dataset.vote = vote || "";
      this.node.dataset.voting = String(Boolean(pending));
      Object.entries(this.voteButtons || {}).forEach(([name, button]) => {
        button.setAttribute("aria-pressed", String(name === vote));
        button.disabled = Boolean(vote);
      });
    }

    label(text) {
      this.node.querySelector(".label").textContent = text;
    }

    /** Fold the body away, leaving the head as the handle that brings it back. */
    collapse(force) {
      const collapsed = force === undefined ? this.node.dataset.collapsed !== "true" : force;
      this.node.dataset.collapsed = String(collapsed);
      this.toggle.setAttribute("aria-expanded", String(!collapsed));
      edges();
    }

    async copy() {
      flash(this.copyButton, await Clipboard.write(this.text));
    }

    append(text) {
      if (!text) return;
      this.text += text;
      this.render();
    }

    set(text) {
      this.text = text || "";
      this.tailing = false;
      this.render();
    }

    /** Append live output, keeping only its tail: a command may print forever. */
    tail(text, limit = 4000) {
      if (!text) return;
      if (!this.tailing) {
        this.tailing = true;
        text = `\n${text}`;
      }
      this.text = (this.text + text).slice(-limit);
      this.render();
    }

    /** Coalesce renders onto a frame: a token arrives far faster than a paint. */
    render() {
      if (this.frame) return;
      this.frame = requestAnimationFrame(() => {
        this.frame = 0;
        this.paint();
      });
    }

    paint() {
      // A tool call is a transcript of what ran, never markdown to interpret.
      if (this.kind === "tool") this.body.textContent = this.text;
      else this.body.innerHTML = Markdown.render(this.text);
    }

    /** Become a question card: the question, then one choice per option. The
     *  card takes over the block of the `ask_user` call that asked it. */
    ask(question, options, pick) {
      if (this.kind !== "question") {
        this.kind = "question";
        this.node.dataset.kind = "question";
        this.node.querySelector(".kind").outerHTML = Icons.markup(KIND_ICONS.question, "icon kind");
      }
      this.label("question");
      this.set(question || "");
      if (this.choices) this.choices.remove();
      this.choices = document.createElement("div");
      this.choices.className = "choices";
      (options || []).forEach((option) => {
        const choice = document.createElement("button");
        choice.type = "button";
        choice.className = "choice";
        choice.textContent = choice.dataset.value = String(option);
        choice.setAttribute("aria-pressed", "false");
        choice.addEventListener("click", () => pick(String(option)));
        this.choices.appendChild(choice);
      });
      this.choices.hidden = !this.choices.children.length;
      this.node.appendChild(this.choices);
    }

    /** Close the card: press the choice that was picked, or show the words. */
    answered(text, ok) {
      this.node.dataset.answered = "true";
      let picked = false;
      [...(this.choices ? this.choices.children : [])].forEach((choice) => {
        const hit = ok && choice.dataset.value === text;
        choice.disabled = true;
        choice.setAttribute("aria-pressed", String(hit));
        picked = picked || hit;
      });
      if (!picked) {
        const reply = document.createElement("div");
        reply.className = "reply";
        reply.dataset.empty = String(!(ok && text));
        reply.innerHTML = Icons.markup("user");
        const words = document.createElement("span");
        words.textContent = ok && text ? text : "no answer";
        reply.appendChild(words);
        this.node.appendChild(reply);
      }
      this.end();
    }

    end() {
      this.streaming = false;
      this.node.dataset.streaming = "false";
      if (this.frame) cancelAnimationFrame(this.frame);
      this.frame = 0;
      this.paint();
      // a finished step folds to its head, so a worker reads as a list of steps
      if (this.nested && this.kind !== "output") this.collapse(true);
    }
  }

  const Blocks = {
    map: new Map(),
    /** Get or create a block: updates may arrive concurrently, out of order.
     *  With `parent` (a worker's step) it is nested in that block when the
     *  page has it, else shown on its own. */
    ensure(id, kind, role, parent) {
      let block = this.map.get(id);
      if (!block) {
        const host = parent ? this.map.get(parent) : null;
        block = new Block(id, kind, role, Boolean(host));
        this.map.set(id, block);
        el.empty.hidden = true;
        if (host) host.nest(block);
        else el.discussion.appendChild(block.node);
        this.scroll();
      }
      return block;
    },
    scroll() {
      const near =
        el.discussion.scrollHeight - el.discussion.scrollTop - el.discussion.clientHeight;
      if (near < 160) el.discussion.scrollTop = el.discussion.scrollHeight;
      edges();
    },
    clear() {
      this.map.clear();
      [...el.discussion.querySelectorAll(".block")].forEach((n) => n.remove());
      el.empty.hidden = false;
      edges();
    },
    note(kind, text) {
      const block = this.ensure(`note-${Date.now()}-${Math.random()}`, kind, "system");
      block.set(text);
      block.end();
      this.scroll();
    },
  };

  // --------------------------------------------------------------- commands

  const Commands = {
    server: [],
    local: {
      clear: {
        description: "Clear the discussion",
        run: () => Blocks.clear(),
      },
      theme: {
        description: "Load a VS Code theme file",
        run: () => Commands.pickTheme(),
      },
    },
    all() {
      return [
        ...this.server,
        ...Object.entries(this.local).map(([name, c]) => ({
          name,
          description: c.description,
        })),
      ].sort((a, b) => a.name.localeCompare(b.name));
    },
    parse(text) {
      const match = /^\/([a-zA-Z][\w-]*)\s*(.*)$/s.exec((text || "").trim());
      return match ? { name: match[1].toLowerCase(), args: match[2].trim() } : null;
    },
    pickTheme() {
      const picker = document.createElement("input");
      picker.type = "file";
      picker.accept = ".json";
      picker.addEventListener("change", async () => {
        const file = picker.files && picker.files[0];
        if (!file) return;
        try {
          Theme.apply(Theme.fromVsCode(JSON.parse(await file.text())));
          Blocks.note("log", `Theme applied: ${file.name}`);
        } catch (err) {
          Blocks.note("error", `Invalid theme file: ${err.message}`);
        }
      });
      picker.click();
    },
    /** Returns true when handled locally, otherwise the server handles it. */
    run(text) {
      const parsed = this.parse(text);
      if (!parsed) return false;
      const local = this.local[parsed.name];
      if (!local) return false;
      local.run(parsed.args);
      return true;
    },
  };

  // ----------------------------------------------------------- attachments

  const Attachments = {
    items: [],
    async add(files) {
      for (const file of files) {
        const data = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onerror = () => reject(reader.error);
          reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
          reader.readAsDataURL(file);
        });
        this.items.push({ name: file.name, type: file.type, data });
      }
      this.render();
    },
    remove(name) {
      this.items = this.items.filter((item) => item.name !== name);
      this.render();
    },
    take() {
      const items = this.items;
      this.items = [];
      this.render();
      return items;
    },
    render() {
      el.attachments.hidden = this.items.length === 0;
      el.attachments.replaceChildren(
        ...this.items.map((item) => {
          const chip = document.createElement("span");
          chip.className = "chip";
          const label = document.createElement("span");
          label.textContent = item.name;
          const close = document.createElement("button");
          close.type = "button";
          close.className = "button tiny";
          close.title = `Remove ${item.name}`;
          close.setAttribute("aria-label", `Remove ${item.name}`);
          close.innerHTML = Icons.markup("close");
          close.addEventListener("click", () => this.remove(item.name));
          chip.append(label, close);
          return chip;
        }),
      );
    },
  };

  // ------------------------------------------------------------------ saved

  /** `localStorage`, which may be barred — a private window, a sandboxed
   *  frame — and the page works without it. An empty value removes the key. */
  const Store = {
    get(key) {
      try {
        return localStorage.getItem(key);
      } catch {
        return null;
      }
    },
    set(key, value) {
      try {
        if (value) localStorage.setItem(key, value);
        else localStorage.removeItem(key);
      } catch {
        // nothing to remember with; the next load starts without it
      }
    },
  };

  /** The last session of this page, so a reload resumes it while it is still
   *  live (the server opens a fresh one when it is not). */
  const Saved = {
    KEY: "agentic.session",
    get() {
      return Store.get(this.KEY) || null;
    },
    set(id) {
      Store.set(this.KEY, id);
    },
  };

  /** The text typed and not yet sent, in the box or the editor alike, so a
   *  reload or a closed tab loses nothing; a submit clears it. */
  const Draft = {
    KEY: "agentic.draft",
    get() {
      return Store.get(this.KEY) || "";
    },
    set(text) {
      Store.set(this.KEY, text);
    },
  };

  // ------------------------------------------------------------------- wait

  /** A silent model's timer beside the mark. The server reports the silence
   *  every few seconds (`model.wait`); the timer ticks on its own in between
   *  and stops at the first sign of the stream. */
  const Wait = {
    timer: 0,
    since: 0,
    start(elapsed, timeout) {
      this.since = Date.now() - (Number(elapsed) || 0) * 1000;
      this.timeout = Number(timeout) || 0;
      el.app.dataset.waiting = "true";
      el.wait.hidden = false;
      this.tick();
      if (!this.timer) this.timer = setInterval(() => this.tick(), 1000);
      Socket.describe();
    },
    tick() {
      const seconds = Math.max(0, Math.floor((Date.now() - this.since) / 1000));
      el.wait.textContent = clock(seconds);
      el.wait.title = this.timeout
        ? `Model silent for ${clock(seconds)} of ${clock(Math.round(this.timeout))}`
        : `Model silent for ${clock(seconds)}`;
    },
    stop() {
      if (!this.timer && el.wait.hidden) return;
      clearInterval(this.timer);
      this.timer = 0;
      el.wait.hidden = true;
      el.app.dataset.waiting = "false";
      Socket.describe();
    },
  };

  // ------------------------------------------------------------------ gauge

  /** A token count for people: `950`, `12k`, `1.2M`. */
  const tokens = (count) => {
    const n = Number(count) || 0;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (n >= 1e4) return `${Math.round(n / 1e3)}k`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
    return String(n);
  };

  /** The context in use (`context.usage`): the mark's core fills like a vial
   *  and a hairline under the bar grows with it; near the point compaction
   *  starts at, both turn warn. Unknown use leaves the core full. */
  const Gauge = {
    usage: null,
    set(usage) {
      if (!usage || !Number(usage.usable)) return;
      this.usage = usage;
      const fill = Math.max(0, Math.min(1, Number(usage.fill) || 0));
      const trigger = Number(usage.high) / Number(usage.usable) || 0.85;
      el.app.style.setProperty("--fill", fill.toFixed(4));
      el.app.dataset.gauge = fill >= trigger * 0.9 ? "high" : fill >= trigger * 0.6 ? "mid" : "low";
      el.gauge.hidden = false;
      Socket.describe();
    },
    clear() {
      this.usage = null;
      el.app.style.removeProperty("--fill");
      delete el.app.dataset.gauge;
      el.gauge.hidden = true;
      Socket.describe();
    },
    /** For the label of the mark: `context 45% (58k of 102k)`. */
    text() {
      const usage = this.usage;
      if (!usage) return "";
      const fill = Math.round(Math.min(1, Number(usage.fill) || 0) * 100);
      const speed = this.speed();
      return (
        `context ${fill}% (${tokens(usage.tokens)} of ${tokens(usage.usable)})` +
        (speed ? `, model ${speed}` : "")
      );
    },
    /** The measured speeds: `reads ~100 · writes ~20 tokens/s`, or nothing. */
    speed() {
      const usage = this.usage || {};
      const parts = [];
      if (Number(usage.prefill) > 0) parts.push(`reads ~${Math.round(usage.prefill)}`);
      if (Number(usage.decode) > 0) parts.push(`writes ~${Math.round(usage.decode)}`);
      return parts.length ? `${parts.join(" · ")} tokens/s` : "";
    },
    /** A compaction block: squeezing while it summarises, its saving once done. */
    compaction(block, data, done) {
      block.label(
        done
          ? `compacted · ${tokens(data.tokens_before)} → ${tokens(data.tokens_after)}`
          : "compacting…",
      );
      el.app.dataset.compacting = String(!done);
      Socket.describe();
    },
  };

  // ------------------------------------------------------------------- plan

  const SVG_NS = "http://www.w3.org/2000/svg";

  /** The plan of the session (`plan.update`, and `hello` for a client that
   *  joins): a ring of steps around the mark, done ones solid and the current
   *  one glowing, and a strip in the bar that opens into the list of steps
   *  with what is left to read and how fast the model reads it. */
  const Plan = {
    view: null,
    //: Past this many steps the ring shows progress, not one arc per step.
    ARCS: 24,
    RADIUS: 11.4,
    STATUS_ICONS: { done: "circle-check", active: "circle-dot", pending: "circle" },
    set(view) {
      this.view = view && Array.isArray(view.steps) && view.steps.length ? view : null;
      const shown = Boolean(this.view);
      el.app.dataset.plan = String(shown);
      el.plan.hidden = !shown;
      if (!shown) this.open(false);
      this.ring();
      this.strip();
      Socket.describe();
    },
    /** One arc of the ring, from angle `a0` to `a1` (radians, clockwise from the top). */
    arc(a0, a1, status) {
      const r = this.RADIUS;
      const full = a1 - a0 >= Math.PI * 2 - 1e-6;
      const node = document.createElementNS(SVG_NS, full ? "circle" : "path");
      if (full) {
        node.setAttribute("cx", "12");
        node.setAttribute("cy", "12");
        node.setAttribute("r", String(r));
      } else {
        const at = (a) => `${(12 + r * Math.sin(a)).toFixed(3)} ${(12 - r * Math.cos(a)).toFixed(3)}`;
        const large = a1 - a0 > Math.PI ? 1 : 0;
        node.setAttribute("d", `M ${at(a0)} A ${r} ${r} 0 ${large} 1 ${at(a1)}`);
      }
      node.dataset.status = status;
      el.markPlan.appendChild(node);
    },
    ring() {
      el.markPlan.textContent = "";
      const view = this.view;
      if (!view) return;
      const steps = view.steps;
      const turn = Math.PI * 2;
      if (steps.length > this.ARCS) {
        const done = (Number(view.done) || 0) / steps.length;
        if (done > 0) this.arc(0, turn * done, "done");
        if (done < 1) this.arc(turn * done, turn, "pending");
        return;
      }
      const span = turn / steps.length;
      const gap = steps.length > 1 ? Math.min(0.35, span * 0.3) : 0;
      steps.forEach((step, index) => {
        const start = index * span + gap / 2;
        this.arc(start, start + span - gap, step.status || "pending");
      });
    },
    strip() {
      const view = this.view;
      el.planList.textContent = "";
      if (!view) return;
      const current = Number(view.current) || 0;
      el.planCount.textContent = `${view.done}/${view.total}`;
      el.planNow.textContent = current ? view.steps[current - 1].title : "done";
      view.steps.forEach((step, index) => {
        const item = document.createElement("li");
        item.dataset.status = step.status || "pending";
        item.dataset.current = String(index + 1 === current);
        item.innerHTML = Icons.markup(this.STATUS_ICONS[step.status] || "circle");
        const title = document.createElement("span");
        title.className = "plan-title";
        title.textContent = step.title;
        item.appendChild(title);
        if (Number(step.tokens) > 0 && step.status !== "done") {
          const size = document.createElement("span");
          size.className = "plan-size";
          size.textContent = tokens(step.tokens);
          item.appendChild(size);
        }
        el.planList.appendChild(item);
      });
      this.foot();
    },
    /** What is left and how fast it is read: `3.4k left · ~2 min · reads ~100 tokens/s`. */
    foot() {
      const view = this.view;
      if (!view) return;
      const parts = [];
      if (Number(view.left) > 0) parts.push(`${tokens(view.left)} tokens left`);
      if (Number(view.seconds) >= 60) parts.push(`~${Math.round(view.seconds / 60)} min`);
      const speed = Gauge.speed();
      if (speed) parts.push(speed);
      // one span a part, so a narrow panel wraps between them, not inside one
      el.planFoot.textContent = "";
      parts.forEach((part) => {
        const span = document.createElement("span");
        span.textContent = part;
        el.planFoot.appendChild(span);
      });
      el.planFoot.hidden = !parts.length;
    },
    open(force) {
      const open = force === undefined ? el.planPanel.hidden : Boolean(force) && Boolean(this.view);
      el.planPanel.hidden = !open;
      el.plan.setAttribute("aria-expanded", String(open));
    },
    /** For the label of the mark: `plan 1 of 3 done, now Write`. */
    text() {
      const view = this.view;
      if (!view) return "";
      const current = Number(view.current) || 0;
      return (
        `plan ${view.done} of ${view.total} done` +
        (current ? `, now ${view.steps[current - 1].title}` : "")
      );
    },
  };

  el.plan.addEventListener("click", (event) => {
    event.stopPropagation();
    Plan.open();
  });
  el.planPanel.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", () => Plan.open(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el.planPanel.hidden) Plan.open(false);
  });

  /** Seconds as `42s`, or `m:ss` once there is a minute of them. */
  const clock = (seconds) =>
    seconds < 60
      ? `${seconds}s`
      : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

  // -------------------------------------------------------------- questions

  /** What the model asked and is still waiting on. A choice on the card
   *  answers its own question; the composer answers the oldest open one. */
  const Questions = {
    open: [],
    ask(message) {
      const id = message.id;
      Blocks.ensure(id, "question", "assistant").ask(message.question, message.options, (text) =>
        this.answer(id, text),
      );
      if (!this.open.includes(id)) this.open.push(id);
      this.update();
      Blocks.scroll();
      // A pointer user is likely to type; on touch the keyboard would hide the choices.
      if (window.matchMedia && matchMedia("(pointer: fine)").matches)
        Editor.field().focus({ preventScroll: true });
    },
    answer(id, text) {
      return Socket.send({ type: "answer", id, text });
    },
    close(message) {
      const block = Blocks.map.get(message.id);
      if (block) block.answered(String(message.answer || ""), message.ok !== false);
      this.forget(message.id);
    },
    forget(id) {
      this.open = id === undefined ? [] : this.open.filter((open) => open !== id);
      this.update();
    },
    update() {
      const asking = this.open.length > 0;
      el.app.dataset.asking = String(asking);
      el.input.placeholder = el.editorText.placeholder = asking
        ? "Answer the agent…"
        : "Message the agent…";
      Socket.controls();
    },
  };

  // --------------------------------------------------------------- feedback

  /** Votes on answers. The server distils a rule from the run behind the
   *  answer and tells every client of the session how it went (`feedback`);
   *  while a rule is being learned the mark turns its core. */
  const Feedback = {
    pending: new Set(),
    send(block, vote) {
      if (block.node.dataset.vote) return;
      if (!Socket.send({ type: "feedback", id: block.id, vote })) return;
      block.voted(vote, true);
      this.pending.add(block.id);
      this.update();
    },
    receive(message) {
      this.pending.delete(message.id);
      this.update();
      const block = Blocks.map.get(message.id);
      const rated = /^Already rated (up|down)/.exec(message.message || "");
      if (block) block.voted(message.ok ? message.vote : rated ? rated[1] : "");
      const warning = message.warning || "";
      const text = String(message.message || "").replace(`\n${warning}`, "");
      Blocks.note(message.ok ? "log" : "error", text || "feedback failed");
      if (warning) Blocks.note("warning", warning);
    },
    update() {
      el.app.dataset.learning = String(this.pending.size > 0);
      Socket.describe();
    },
  };

  // ----------------------------------------------------------------- resume

  /** The run that did not finish (a crash, a failure, a stop) offers to carry
   *  on from its prompt; the journal on the server holds what it had done. */
  const Resume = {
    button: null,
    offer(block) {
      this.withdraw();
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button tiny resume";
      button.title = "Resume this run";
      button.setAttribute("aria-label", "Resume this run");
      button.innerHTML = Icons.markup("play");
      button.addEventListener("click", () => {
        if (Socket.send({ type: "command", text: "/resume" })) this.withdraw();
      });
      block.node.querySelector(".block-actions").prepend(button);
      this.button = button;
    },
    withdraw() {
      if (this.button) this.button.remove();
      this.button = null;
    },
  };

  // ----------------------------------------------------------------- socket

  const Socket = {
    ws: null,
    session: Saved.get(),
    retry: 0,
    name: "agent",
    connect() {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      this.setState("connecting");
      this.ws = new WebSocket(`${scheme}://${location.host}/ws`);
      this.ws.addEventListener("open", () => {
        this.retry = 0;
        this.setState("online");
        this.send({ type: "hello", session: this.session });
      });
      this.ws.addEventListener("close", () => {
        // The server cancels the run of a closed socket, and its questions too.
        Questions.forget();
        Wait.stop();
        this.setState("offline");
        this.retry = Math.min(this.retry + 1, 6);
        setTimeout(() => this.connect(), 250 * 2 ** this.retry);
      });
      this.ws.addEventListener("message", (event) => {
        let message;
        try {
          message = JSON.parse(event.data);
        } catch {
          return;
        }
        this.receive(message);
      });
    },
    /** The mark carries the state; the text is kept for screen readers only. */
    setState(state) {
      el.app.dataset.state = state;
      const words = {
        online: "connected",
        offline: "disconnected",
        connecting: "connecting",
      };
      el.status.textContent = words[state] || state;
      this.describe();
      const offline = state !== "online";
      el.input.disabled = offline;
      el.send.disabled = offline;
      if (!offline) Editor.field().focus({ preventScroll: true });
    },
    describe() {
      const state = el.app.dataset.state;
      const working =
        el.app.dataset.asking === "true"
          ? ", waiting for your answer"
          : el.app.dataset.compacting === "true"
            ? ", compacting the context"
            : el.app.dataset.waiting === "true"
              ? ", model writing"
              : el.app.dataset.busy === "true"
                ? ", working"
                : el.app.dataset.learning === "true"
                  ? ", learning a rule"
                  : "";
      const words = {
        online: "connected",
        offline: "disconnected",
        connecting: "connecting",
      };
      const context = Gauge.text();
      const plan = Plan.text();
      const label =
        `${this.name}, ${words[state] || state}${working}` +
        `${context ? `, ${context}` : ""}${plan ? `, ${plan}` : ""}`;
      el.brand.setAttribute("aria-label", label);
      el.brand.title = label.replace(", ", " — ");
    },
    send(message) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify(message));
        return true;
      }
      Blocks.note("error", "Not connected.");
      return false;
    },
    busy(active) {
      el.app.dataset.busy = String(Boolean(active));
      this.controls();
    },
    /** Stop while a run is in flight; send when idle, or when it asked. */
    controls() {
      const busy = el.app.dataset.busy === "true";
      el.stop.hidden = !busy;
      el.send.hidden = busy && el.app.dataset.asking !== "true";
      this.describe();
    },
    /** Pop the mark once: a running tool just reported that it is alive. */
    beat() {
      el.app.dataset.beat = "false";
      void el.brand.offsetWidth; // restart the animation on every beat
      el.app.dataset.beat = "true";
    },
    receive(message) {
      // Anything the run does is a sign the model is no longer silent.
      if (/^(block|tool|agent|question)\.|^(cancelled|error)$/.test(message.type)) Wait.stop();
      switch (message.type) {
        case "hello": {
          const asked = this.session;
          this.session = message.session && message.session.id;
          Saved.set(this.session);
          el.session.textContent = this.session ? `#${this.session}` : "";
          this.name = (message.config && message.config.name) || "agent";
          this.describe();
          Commands.server = message.commands || [];
          Theme.apply(message.theme);
          Gauge.clear();
          Gauge.set(message.context);
          Plan.set(message.plan);
          // The journal, then the run in flight where it is now, then what it asked.
          this.replay(message.history, message.unfinished);
          (message.live || []).forEach((live) => this.revive(live));
          (message.questions || []).forEach((question) => Questions.ask(question));
          this.busy(Boolean(message.busy));
          if (asked && asked !== this.session)
            Blocks.note("log", `Session \`#${asked}\` has ended; this is a new one.`);
          // once per page: a flaky connection must not stack the same warning
          if (message.warning && !this.warned) Blocks.note("warning", message.warning);
          this.warned = this.warned || Boolean(message.warning);
          break;
        }
        case "block.start":
          if (message.kind === "prompt") Resume.withdraw();
          Blocks.ensure(message.id, message.kind, message.role, message.parent).set(
            message.text || "",
          );
          if (message.attachments && message.attachments.length) {
            Blocks.map
              .get(message.id)
              .append(
                `\n\n${message.attachments.map((name) => `- \`${name}\``).join("\n")}`,
              );
          }
          if (message.complete) Blocks.map.get(message.id).end();
          if (message.kind === "prompt") this.busy(true);
          if (message.kind === "compact" && !message.parent)
            Gauge.compaction(Blocks.map.get(message.id), message, false);
          break;
        case "block.delta":
          Blocks.ensure(message.id, message.kind, undefined, message.parent).append(
            message.text || "",
          );
          Blocks.scroll();
          break;
        case "tool.start":
        case "tool.end": {
          const tool = Blocks.ensure(message.id, "tool", undefined, message.parent);
          // A question card keeps its own face; the call only opens and ends it.
          if (tool.kind !== "question") {
            tool.label(message.name || "tool");
            tool.set(message.text || "");
          }
          if (message.type === "tool.end") {
            tool.node.dataset.status = message.ok === false ? "failed" : "ok";
            tool.end();
          }
          Blocks.scroll();
          break;
        }
        case "tool.progress": {
          // Output to tail, or an empty heartbeat: either way the call is alive.
          const tool = Blocks.ensure(message.id, "tool", undefined, message.parent);
          const seconds = Math.round(Number(message.duration) || 0);
          tool.label(`${message.name || "tool"} · ${seconds}s`);
          tool.tail(message.text || "");
          this.beat();
          Blocks.scroll();
          break;
        }
        case "block.end": {
          const block = Blocks.ensure(message.id, message.kind, undefined, message.parent);
          if (message.kind === "compact") {
            block.set(message.text || "");
            if (message.parent) block.label("compacted");
            else Gauge.compaction(block, message, true);
            block.collapse(true);
          }
          block.end();
          break;
        }
        case "context.usage":
          Gauge.set(message);
          Plan.foot();
          break;
        case "plan.update":
          Plan.set(message.plan);
          break;
        case "model.wait":
          // The model is writing something it sends whole (a tool call on
          // Ollama): pulse the mark and count the silence.
          Wait.start(message.elapsed, message.timeout);
          this.beat();
          break;
        case "question.start":
          Questions.ask(message);
          break;
        case "question.end":
          Questions.close(message);
          break;
        case "feedback":
          Feedback.receive(message);
          break;
        case "model.retry":
          Blocks.note(
            "error",
            `${message.role || "leader"} model ${message.error_kind || "error"}: ` +
            `retry ${(Number(message.attempt) || 1) + 1}/${Number(message.attempts) || 1} ` +
            `in ${(Number(message.delay) || 0).toFixed(1)}s`,
          );
          break;
        case "agent.end":
          this.busy(false);
          if (message.error) Blocks.note("error", message.error);
          break;
        case "agent.error":
          Blocks.note("error", String(message.error || "unknown error"));
          this.busy(false);
          break;
        case "command":
          this.applyCommand(message.result);
          break;
        case "cancelled":
          this.busy(false);
          break;
        case "error":
          Blocks.note("error", String(message.message || "error"));
          this.busy(false);
          break;
        default:
          break;
      }
    },
    /** Render the journal the server keeps, replacing what is shown. A turn
     *  keeps the id of the block it was journaled from, its vote and, on a
     *  prompt, how its run ended; the run that did not finish offers resume. */
    replay(history, unfinished) {
      if (!Array.isArray(history) || !history.length) return;
      Blocks.clear();
      Resume.withdraw();
      history.forEach((turn, index) => {
        const meta = turn.meta || {};
        const block = Blocks.ensure(
          turn.id || `memory-${index}`,
          turn.kind || "output",
          turn.role || "assistant",
        );
        if (turn.kind === "tool") {
          block.label(meta.name || "tool");
          block.node.dataset.status = meta.ok === false ? "failed" : "ok";
        }
        block.set(turn.text || "");
        if (turn.kind === "compact") {
          Gauge.compaction(block, meta, true);
          block.collapse(true);
        }
        block.end();
        // the steps its workers took, nested in the call again (finished, so folded)
        (meta.steps || []).forEach((step) => this.revive({ ...step, done: true }));
        if (meta.vote) block.voted(meta.vote);
        // a run still journaled as running is either live (left plain) or unfinished
        const stopped = meta.status && meta.status !== "ok" && meta.status !== "running";
        if (turn.kind === "prompt" && (stopped || (unfinished && turn.run === unfinished)))
          block.node.dataset.status = meta.status;
        if (unfinished && turn.run === unfinished && turn.kind === "prompt") Resume.offer(block);
      });
      Blocks.scroll();
    },
    /** A block of the run in flight, as it stands when this client joins, or
     *  a worker's step (`parent`), live or journaled on its call. */
    revive(live) {
      const block = Blocks.ensure(live.id, live.kind, live.role, live.parent);
      if (live.kind === "tool") {
        block.label(live.name || "tool");
        if (live.done) block.node.dataset.status = live.ok === false ? "failed" : "ok";
      }
      if (live.kind === "compact" && live.parent) block.label("compacted");
      block.set(live.text || "");
      if (live.done) block.end();
      Blocks.scroll();
    },
    applyCommand(result) {
      if (!result) return;
      // a vote is reported by its `feedback` message, to every client
      if (result.vote) return;
      if ("plan" in result) Plan.set(result.plan);
      if (result.session && result.session.id) {
        if (result.session.id !== this.session) {
          Gauge.clear();
          Plan.set(null);
        }
        this.session = result.session.id;
        Saved.set(this.session);
        el.session.textContent = `#${this.session}`;
      } else if (result.session === null && result.ok) {
        Saved.set(null); // ended: a reload has nothing to resume
      }
      if (result.commands) {
        Blocks.note(
          "log",
          Commands.all()
            .map((c) => `- \`/${c.name}\` — ${c.description}`)
            .join("\n"),
        );
        return;
      }
      if (result.sessions) {
        const list = result.sessions.length
          ? result.sessions.map((s) => `- \`${s.id}\``).join("\n")
          : "_no live sessions_";
        Blocks.note("log", list);
        return;
      }
      Blocks.note(result.ok ? "log" : "error", result.message || "done");
    },
  };

  // -------------------------------------------------------------- composer

  /** Grow the box with the text it holds, up to the cap the stylesheet sets. */
  const resize = () => {
    const limit = parseFloat(getComputedStyle(el.input).maxHeight);
    const max = Number.isFinite(limit) ? limit : 176;
    el.input.style.height = "auto";
    const wanted = el.input.scrollHeight;
    el.input.style.height = `${Math.min(wanted, max)}px`;
    el.input.style.overflowY = wanted > max ? "auto" : "hidden";
  };

  const hints = {
    index: 0,
    items: [],
    show(prefix) {
      this.items = Commands.all().filter((c) => c.name.startsWith(prefix));
      this.index = 0;
      el.hints.hidden = this.items.length === 0;
      el.hints.replaceChildren(
        ...this.items.map((command, i) => {
          const row = document.createElement("div");
          row.className = "hint";
          row.setAttribute("aria-selected", String(i === this.index));
          const name = document.createElement("b");
          name.textContent = `/${command.name}`;
          const description = document.createElement("em");
          description.textContent = command.description;
          row.append(name, description);
          row.addEventListener("mousedown", (event) => {
            event.preventDefault();
            this.pick(i);
          });
          el.hints.appendChild(row);
          return row;
        }),
      );
    },
    move(delta) {
      if (!this.items.length) return;
      this.index = (this.index + delta + this.items.length) % this.items.length;
      [...el.hints.children].forEach((row, i) =>
        row.setAttribute("aria-selected", String(i === this.index)),
      );
    },
    pick(index) {
      const command = this.items[index === undefined ? this.index : index];
      if (!command) return;
      el.input.value = `/${command.name} `;
      Draft.set(el.input.value);
      this.hide();
      el.input.focus();
      resize();
    },
    hide() {
      this.items = [];
      el.hints.hidden = true;
      el.hints.replaceChildren();
    },
  };

  // ----------------------------------------------------------------- editor

  /** What one press of Tab indents by. */
  const INDENT = "  ";

  /** The composer lifted into a markdown editor over the discussion. The box
   *  and the editor hold one draft between them: the one on screen owns it,
   *  and hands it, with the caret, to the other on every switch. */
  const Editor = {
    lifted: false,
    /** The field the draft is typed into right now. */
    field() {
      return this.lifted ? el.editorText : el.input;
    },
    lift() {
      if (this.lifted) return;
      this.swap(el.input, el.editorText, true);
      hints.hide();
      this.paint();
    },
    /** Back to the discussion. The box takes the focus only from a pointer:
     *  on touch the keyboard would cover what the reader came back to see. */
    drop() {
      if (!this.lifted) return;
      const fine = Boolean(window.matchMedia && matchMedia("(pointer: fine)").matches);
      this.swap(el.editorText, el.input, fine);
      resize();
      edges();
    },
    swap(from, to, focus) {
      const { selectionStart, selectionEnd } = from;
      to.value = from.value;
      this.lifted = to === el.editorText;
      el.app.dataset.view = this.lifted ? "editor" : "discussion";
      if (!focus) return;
      to.focus({ preventScroll: true });
      to.setSelectionRange(selectionStart, selectionEnd);
    },
    /** Empty both fields after a submit and go back to watch the answer. */
    clear() {
      el.input.value = el.editorText.value = "";
      Draft.set("");
      this.paint();
      this.drop();
      resize();
      hints.hide();
    },
    /** Repaint the highlighted layer under the textarea. It is cheap enough
     *  to do on every keystroke, and a frame late would show a blank glyph. */
    paint() {
      el.editorPaint.innerHTML = Syntax.highlight(el.editorText.value);
      this.sync();
    },
    /** Keep the layer under the textarea scrolled with it. */
    sync() {
      el.editorPaint.style.transform = `translateY(${-el.editorText.scrollTop}px)`;
    },
    /** Replace the selection the way typing would: one undo step, one input
     *  event. `execCommand` is deprecated but still the only way to keep the
     *  native undo stack; `setRangeText` is the fallback. */
    insert(text) {
      const area = el.editorText;
      area.focus();
      let done = false;
      try {
        done = text
          ? document.execCommand("insertText", false, text)
          : document.execCommand("delete", false);
      } catch {
        done = false;
      }
      if (!done) {
        area.setRangeText(text, area.selectionStart, area.selectionEnd, "end");
        area.dispatchEvent(new Event("input"));
      }
    },
    /** Enter on a list item or a quote starts the next one; on an empty one
     *  it ends the list instead. Returns whether it handled the key. */
    newline() {
      const area = el.editorText;
      const { value, selectionStart: start, selectionEnd: end } = area;
      if (start !== end) return false;
      const from = value.lastIndexOf("\n", start - 1) + 1;
      const before = value.slice(from, start);
      const item = /^(\s*)(?:([-*+])|(\d{1,9})([.)]))(\s+)(\[[ xX]\]\s+)?/.exec(before);
      const quote = item ? null : /^\s*(?:>\s?)+/.exec(before);
      const lead = item || quote;
      if (!lead) return false;
      const stop = value.indexOf("\n", start);
      const after = value.slice(start, stop < 0 ? value.length : stop);
      if (!before.slice(lead[0].length).trim() && !after.trim()) {
        area.setSelectionRange(from, start);
        this.insert("");
        return true;
      }
      const next = item
        ? item[1] + (item[2] || `${Number(item[3]) + 1}${item[4]}`) + item[5] + (item[6] ? "[ ] " : "")
        : quote[0];
      this.insert(`\n${next}`);
      return true;
    },
    /** Tab indents the lines under the selection, Shift+Tab outdents them; a
     *  bare caret on Tab only inserts the indent. */
    indent(outdent) {
      const area = el.editorText;
      const { value, selectionStart: start, selectionEnd: end } = area;
      if (!outdent && start === end) {
        this.insert(INDENT);
        return;
      }
      const from = value.lastIndexOf("\n", start - 1) + 1;
      // a selection that ends at the start of a line leaves that line alone
      const last = end > start && value[end - 1] === "\n" ? end - 1 : end;
      const newline = value.indexOf("\n", last);
      const stop = newline < 0 ? value.length : newline;
      const lines = value.slice(from, stop).split("\n");
      const shifted = lines.map((text) =>
        outdent ? text.replace(/^( {1,2}|\t)/, "") : INDENT + text,
      );
      const first = shifted[0].length - lines[0].length;
      const total = shifted.join("\n").length - (stop - from);
      if (!total) return;
      area.setSelectionRange(from, stop);
      this.insert(shifted.join("\n"));
      area.setSelectionRange(Math.max(from, start + first), Math.max(from, end + total));
    },
    async copy() {
      flash(el.editorCopy, await Clipboard.write(el.editorText.value));
    },
  };

  const submit = () => {
    const text = Editor.field().value.trim();
    if (!text) return;
    if (Commands.run(text)) {
      Editor.clear();
      return;
    }
    // While the model waits on a question, what is typed is the answer.
    const message = Commands.parse(text)
      ? { type: "command", text }
      : Questions.open.length
        ? { type: "answer", id: Questions.open[0], text }
        : { type: "prompt", text, attachments: Attachments.take() };
    if (Socket.send(message)) Editor.clear();
  };

  el.form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });

  el.input.addEventListener("input", () => {
    resize();
    Draft.set(el.input.value);
    const match = /^\/([a-zA-Z-]*)$/.exec(el.input.value);
    if (match) hints.show(match[1].toLowerCase());
    else hints.hide();
  });

  el.input.addEventListener("keydown", (event) => {
    if (!el.hints.hidden) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        hints.move(event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (event.key === "Tab" || (event.key === "Enter" && hints.items.length)) {
        event.preventDefault();
        hints.pick();
        return;
      }
      if (event.key === "Escape") hints.hide();
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });

  el.file.addEventListener("change", async () => {
    await Attachments.add([...el.file.files]);
    el.file.value = "";
  });

  el.stop.addEventListener("click", () => Socket.send({ type: "cancel" }));

  el.lift.addEventListener("click", () => Editor.lift());
  el.drop.addEventListener("click", () => Editor.drop());
  el.editorCopy.addEventListener("click", () => Editor.copy());

  el.editorText.addEventListener("input", () => {
    Editor.paint();
    Draft.set(el.editorText.value);
  });

  el.editorText.addEventListener("scroll", () => Editor.sync(), { passive: true });

  // Enter is a newline here, so sending takes Ctrl or Cmd; Escape drops back.
  el.editorText.addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    const chord = event.ctrlKey || event.metaKey;
    if (event.key === "Enter" && chord) {
      event.preventDefault();
      submit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      Editor.drop();
    } else if (event.key === "Tab" && !chord && !event.altKey) {
      event.preventDefault();
      Editor.indent(event.shiftKey);
    } else if (event.key === "Enter" && !event.shiftKey && !event.altKey) {
      if (Editor.newline()) event.preventDefault();
    }
  });

  el.discussion.addEventListener("scroll", edges, { passive: true });
  // The view shrinks when the composer grows, which moves both edges too.
  if (window.ResizeObserver) new ResizeObserver(edges).observe(el.discussion);

  el.input.value = Draft.get();
  edges();
  resize();
  Socket.connect();

  // The subsystems, for a test harness or a console: everything the page does
  // is reachable without a socket.
  window.agentUI = {
    Icons,
    Markdown,
    Syntax,
    Media,
    Clipboard,
    Theme,
    Blocks,
    Commands,
    Questions,
    Feedback,
    Resume,
    Wait,
    Saved,
    Draft,
    Editor,
    Socket,
  };
})();
