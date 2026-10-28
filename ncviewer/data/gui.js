// The helpers of the ncviewer page, copied from OceanVal's window (oceanval/data/gui.js):
// el(), the icons, toasts, telling the app a tab is hidden, and withChoices, the list of
// choices a box can have (the directories down box's 0 to 5).
const GUI = (() => {
  "use strict";

  const ICONS = {
    warn: '<svg viewBox="0 0 20 20"><path d="M10 2.6 1.9 16.8h16.2L10 2.6Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M10 8v3.8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="10" cy="14.2" r="1" fill="currentColor"/></svg>',
    error: '<svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="7.6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="m7.4 7.4 5.2 5.2m0-5.2-5.2 5.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    check: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.3 4.3L19 7.2" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    cross: '<svg viewBox="0 0 24 24"><path d="m7 7 10 10M17 7 7 17" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>',
    // the i in a circle: the About button, and the buttons that say what an option plots
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 17v-6M12 7.5h.01" stroke-width="2.6"/></svg>',
  };

  const $ = (id) => document.getElementById(id);

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value; // only ever one of the icons here
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children) {
      if (child !== null && child !== undefined && child !== false) node.append(child);
    }
    return node;
  }

  const icon = (name) => el("span", { class: "icon", "aria-hidden": "true", html: ICONS[name] });

  // long paths keep their start and their file name
  function shorten(text, max = 56) {
    if (text.length <= max) return text;
    const head = Math.floor((max - 1) * 0.35);
    return text.slice(0, head) + "…" + text.slice(text.length - (max - 1 - head));
  }

  function plural(count, word) {
    return `${count} ${word}${count === 1 ? "" : "s"}`;
  }

  let toastTimer = null;
  function toast(text) {
    const node = $("toast");
    node.textContent = text;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 7000);
  }

  // names this page load to ncviewer, which quits when the browser
  // has closed every page it knows of (see App.touch)
  const pageId = Math.random().toString(36).slice(2) + Date.now().toString(36);

  // tells the app when this page's tab is not looked at, and when it is
  // again, as a browser may then slow the page's requests or freeze it
  // (Safari does), which the app would otherwise take for a closed window
  // (see App.page_visibility). keepalive, so that it is sent as the tab goes
  function tellVisibility(token, root) {
    const tell = () => {
      fetch(`${root}api/visibility`, {
        method: "POST",
        cache: "no-store",
        keepalive: true,
        headers: { "Content-Type": "application/json", "X-NCViewer-Token": token },
        body: JSON.stringify({ page: pageId, hidden: document.hidden }),
      }).catch(() => {});
    };
    document.addEventListener("visibilitychange", tell);
    if (document.hidden) tell();
  }

  // A box with yearChoices (years, or names: see withChoices) has a list of
  // them to pick from, and anything can still be typed in it. One list serves
  // every such box, placed under (or over) the box it was opened from, and
  // fixed, so that a pop-out's body cannot clip it. A box's choiceHints are
  // shown beside the choices they name, and its onPick(value) is called once
  // a choice is put in it.
  let choiceList = null;
  let choiceOwner = null;
  let choiceActive = -1;

  function withChoices(input, choices) {
    input.yearChoices = choices;
    input.classList.add("has-years");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-haspopup", "listbox");
    input.setAttribute("aria-expanded", "false");
    return input;
  }

  const choicesOpen = () => Boolean(choiceList) && !choiceList.hidden;

  function closeChoices() {
    if (choiceOwner) choiceOwner.setAttribute("aria-expanded", "false");
    choiceOwner = null;
    choiceActive = -1;
    if (choiceList) choiceList.hidden = true;
  }

  function markChoice(index) {
    const options = [...choiceList.children];
    choiceActive = (index + options.length) % options.length;
    options.forEach((option, number) => {
      option.classList.toggle("is-active", number === choiceActive);
      option.setAttribute("aria-selected", String(number === choiceActive));
    });
    options[choiceActive].scrollIntoView({ block: "nearest" });
  }

  function pickChoice(choice) {
    const input = choiceOwner;
    closeChoices();
    input.value = String(choice);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    if (input.onPick) input.onPick(input.value);
    input.focus();
  }

  function openChoices(input) {
    if (!input.yearChoices || !input.yearChoices.length || input.disabled) return;
    if (!choiceList) {
      choiceList = el("ul", { class: "combo__list year-list", role: "listbox", hidden: true });
      document.body.append(choiceList);
    }
    choiceOwner = input;
    input.setAttribute("aria-expanded", "true");
    // the list is also the preset names of a variable's name (input.onPick), which are not years
    choiceList.classList.toggle("is-names", Boolean(input.onPick));
    // with hints beside the choices, they are in columns, from the left
    choiceList.classList.toggle("is-hinted", Boolean(input.choiceHints));
    choiceList.replaceChildren(...input.yearChoices.map((choice) => el("li", {
      class: "combo__option", role: "option", "aria-selected": "false", text: String(choice),
      // mousedown, so the box keeps its focus
      onmousedown: (event) => { event.preventDefault(); pickChoice(choice); },
    }, (input.choiceHints || {})[choice] ? el("span", { class: "combo__hint", text: input.choiceHints[choice] }) : null)));
    const box = input.getBoundingClientRect();
    const below = window.innerHeight - box.bottom;
    choiceList.style.minWidth = `${box.width}px`;
    choiceList.style.left = `${box.left}px`;
    choiceList.style.top = below >= 200 || below >= box.top ? `${box.bottom + 4}px` : "auto";
    choiceList.style.bottom = below >= 200 || below >= box.top ? "auto" : `${window.innerHeight - box.top + 4}px`;
    choiceList.hidden = false;
    // a list wider than its box, beside the window's edge, is moved in
    const wide = choiceList.getBoundingClientRect().width;
    choiceList.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - wide - 8))}px`;
    // a blank box has none chosen (Number("") is 0, a choice of a depth's)
    const typed = input.value.trim();
    const at = typed === "" ? -1 : input.yearChoices.indexOf(input.onPick ? typed : Number(typed));
    if (at >= 0) markChoice(at);
  }

  // the arrow is the right-hand end of the box: a click there opens the list, one anywhere else in the box only closes it
  document.addEventListener("click", (event) => {
    const input = event.target;
    if (!input.yearChoices) return;
    const open = choicesOpen() && choiceOwner === input;
    if (event.clientX > input.getBoundingClientRect().right - 24) {
      if (open) closeChoices();
      else { closeChoices(); openChoices(input); }
      input.focus();
    } else if (open) closeChoices();
  });
  document.addEventListener("mousedown", (event) => {
    if (choicesOpen() && event.target !== choiceOwner && !choiceList.contains(event.target)) closeChoices();
  });
  document.addEventListener("keydown", (event) => {
    const input = event.target;
    if (!input.yearChoices) return;
    if (!choicesOpen() || choiceOwner !== input) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        closeChoices();
        openChoices(input);
      }
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      markChoice(choiceActive + (event.key === "ArrowDown" ? 1 : -1));
    } else if (event.key === "Enter" && choiceActive >= 0) {
      event.preventDefault();
      pickChoice(input.yearChoices[choiceActive]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeChoices();
    } else if (event.key === "Tab") {
      closeChoices();
    }
  });
  document.addEventListener("input", (event) => { if (choicesOpen() && event.target === choiceOwner) closeChoices(); });
  // a list left behind when the box moves
  document.addEventListener("scroll", (event) => { if (choicesOpen() && event.target !== choiceList) closeChoices(); }, true);
  window.addEventListener("resize", closeChoices);

  return { ICONS, $, el, icon, shorten, plural, toast, pageId, tellVisibility, withChoices, choicesOpen, closeChoices };
})();
