"use strict";

const sessions = {
  claude: {
    source: "IN / CLAUDE CODE SESSION",
    quote:
      "“Keep the API stateless. Ship CSV export first; live integrations can wait until the team has tested the workflow.”",
    caption: "The first session establishes a decision and a clear scope.",
    status: "Architecture set",
    decision:
      "Keep the API stateless so web requests and background jobs share the same contract.",
    changed:
      "CSV export is the first milestone. Live integrations are outside the initial scope.",
    next: "Implement the CSV export flow for shared research.",
  },
  codex: {
    source: "IN / CODEX SESSION",
    quote:
      "“CSV export is ready. Next, verify that invited teammates can access shared research.”",
    caption: "New work lands. The earlier architecture decision still matters.",
    status: "Export ready",
    decision:
      "Keep the API stateless so web requests and background jobs share the same contract.",
    changed:
      "CSV export is complete. Live integrations stay out of scope for now.",
    next: "Verify access for invited teammates before widening the rollout.",
  },
  next: {
    source: "IN / YOUR NEXT SESSION",
    quote:
      "“Read the project context, then help me verify access for invited teammates.”",
    caption:
      "The next tool receives the handoff: the decision, the completed work, and a place to start.",
    status: "Ready to continue",
    decision:
      "Keep the API stateless so web requests and background jobs share the same contract.",
    changed:
      "CSV export is complete. Live integrations stay out of scope for now.",
    next: "Verify access for invited teammates before widening the rollout.",
  },
};

const platforms = {
  unix: {
    command: "curl -fsSL https://gyrus.sh/install | bash",
    shell: "TERMINAL",
    detail:
      "The installer sets up Python through uv and walks you through configuration.",
    source: "https://github.com/prismindanalytics/gyrus/blob/main/install.sh",
  },
  windows: {
    command:
      "git clone https://github.com/prismindanalytics/gyrus.git\ncd gyrus\n.\\install.ps1",
    shell: "POWERSHELL",
    detail:
      "Run these commands in PowerShell with Git installed. The installer walks you through Python, models, and scheduling.",
    source: "https://github.com/prismindanalytics/gyrus/blob/main/install.ps1",
  },
};
let activePlatform = "unix";
let noticeTimer;

function wireTabs(selector, onSelect) {
  const tabs = [...document.querySelectorAll(selector)];
  function select(tab, focus = false) {
    tabs.forEach((item) => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
    });
    onSelect(tab);
    if (focus) tab.focus();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (event) => {
      const vertical =
        tab.parentElement.getAttribute("aria-orientation") === "vertical";
      const previous = vertical ? "ArrowUp" : "ArrowLeft";
      const next = vertical ? "ArrowDown" : "ArrowRight";
      let target;
      if (event.key === previous)
        target = (index - 1 + tabs.length) % tabs.length;
      if (event.key === next) target = (index + 1) % tabs.length;
      if (event.key === "Home") target = 0;
      if (event.key === "End") target = tabs.length - 1;
      if (target !== undefined) {
        event.preventDefault();
        select(tabs[target], true);
      }
    });
  });
}

wireTabs("[data-session]", (tab) => {
  const session = sessions[tab.dataset.session];
  document
    .getElementById("session-panel")
    .setAttribute("aria-labelledby", tab.id);
  const content = {
    "source-label": session.source,
    "source-quote": session.quote,
    "source-caption": session.caption,
    "card-status": session.status,
    "card-decision": session.decision,
    "card-changed": session.changed,
    "card-next": session.next,
  };
  Object.entries(content).forEach(([id, text]) => {
    document.getElementById(id).textContent = text;
  });
});

wireTabs("[data-platform]", (tab) => {
  activePlatform = tab.dataset.platform;
  const platform = platforms[activePlatform];
  document
    .getElementById("install-panel")
    .setAttribute("aria-labelledby", tab.id);
  document.getElementById("install-command").textContent = platform.command;
  document.getElementById("shell-label").textContent = platform.shell;
  document.getElementById("install-detail").textContent = platform.detail;
  document.getElementById("inspect-installer").href = platform.source;
});

function showNotice(message) {
  const status = document.getElementById("copy-status");
  clearTimeout(noticeTimer);
  status.textContent = message;
  status.classList.add("visible");
  noticeTimer = setTimeout(() => status.classList.remove("visible"), 3500);
}

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const text =
      button.dataset.copy === "context"
        ? 'gyrus context --cwd "$PWD" --tool codex'
        : platforms[activePlatform].command;
    try {
      await navigator.clipboard.writeText(text);
      showNotice("Command copied. Ready for your terminal.");
    } catch {
      // Keep the real command selectable when clipboard permissions are denied.
      const command =
        button.dataset.copy === "context"
          ? document.querySelector(".workflow-command code")
          : document.getElementById("install-command");
      const range = document.createRange();
      range.selectNodeContents(command);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      showNotice("Select and copy the command with your keyboard.");
    }
  });
});
