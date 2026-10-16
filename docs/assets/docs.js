(() => {
  "use strict";

  const platformTabs = Array.from(document.querySelectorAll("[data-platform]"));
  const tabList = document.querySelector(".install-tabs");
  const platformPanels = platformTabs.map((tab) =>
    document.getElementById(tab.getAttribute("aria-controls")),
  );

  function selectPlatform(tab) {
    platformTabs.forEach((item, index) => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      platformPanels[index].hidden = !selected;
    });
  }

  if (tabList && platformTabs.length && platformPanels.every(Boolean)) {
    document.documentElement.classList.add("enhanced");
    tabList.hidden = false;
    platformPanels.forEach((panel, index) => {
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", platformTabs[index].id);
      panel.tabIndex = 0;
    });
    selectPlatform(platformTabs[0]);
    platformTabs.forEach((tab, index) => {
      tab.addEventListener("click", () => selectPlatform(tab));
      tab.addEventListener("keydown", (event) => {
        let next;
        if (event.key === "ArrowRight")
          next = (index + 1) % platformTabs.length;
        if (event.key === "ArrowLeft")
          next = (index - 1 + platformTabs.length) % platformTabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = platformTabs.length - 1;
        if (next === undefined) return;
        event.preventDefault();
        selectPlatform(platformTabs[next]);
        platformTabs[next].focus();
      });
    });
  }

  const copyStatus = document.querySelector(".copy-status");
  let statusTimeout;
  function announce(message) {
    if (!copyStatus) return;
    window.clearTimeout(statusTimeout);
    copyStatus.textContent = message;
    copyStatus.classList.add("is-visible");
    statusTimeout = window.setTimeout(
      () => copyStatus.classList.remove("is-visible"),
      4500,
    );
  }

  document.querySelectorAll("[data-copy]").forEach((button) => {
    const code = document.getElementById(button.dataset.copy);
    if (!code) return;
    button.hidden = false;
    button.addEventListener("click", async () => {
      try {
        if (!navigator.clipboard || !navigator.clipboard.writeText)
          throw new Error("Clipboard unavailable");
        await navigator.clipboard.writeText(code.textContent.trim());
        announce("Command copied.");
      } catch {
        const selection = window.getSelection();
        if (selection) {
          const range = document.createRange();
          range.selectNodeContents(code);
          selection.removeAllRanges();
          selection.addRange(range);
          announce("Command selected. Use your keyboard’s copy shortcut.");
        } else {
          announce(
            "Copy is unavailable. Select the command to copy it manually.",
          );
        }
      }
    });
  });

  const contents = document.querySelector(".contents");
  const mobileLayout = window.matchMedia("(max-width: 850px)");
  if (contents) {
    const updateContents = () => {
      contents.open = !mobileLayout.matches;
    };
    updateContents();
    mobileLayout.addEventListener("change", updateContents);
    contents.querySelectorAll("a").forEach((link) => {
      link.addEventListener("click", () => {
        if (mobileLayout.matches) contents.open = false;
      });
    });
  }

  const sectionLinks = Array.from(document.querySelectorAll(".contents nav a"));
  const sections = sectionLinks
    .map((link) => document.querySelector(link.getAttribute("href")))
    .filter(Boolean);
  let scheduled = false;
  function markCurrentSection() {
    let current = sections[0];
    sections.forEach((section) => {
      if (section.getBoundingClientRect().top <= 140) current = section;
    });
    sectionLinks.forEach((link) => {
      if (current && link.getAttribute("href") === `#${current.id}`)
        link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    });
    scheduled = false;
  }
  window.addEventListener(
    "scroll",
    () => {
      if (!scheduled) {
        scheduled = true;
        window.requestAnimationFrame(markCurrentSection);
      }
    },
    { passive: true },
  );
  markCurrentSection();
})();
