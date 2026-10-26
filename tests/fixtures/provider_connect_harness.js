// Runs the real static/provider-connect.js against a minimal browser stand-in and prints
// what it did as JSON. The stand-in records calls; it never decides the script's behavior.
"use strict";
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");

function scenario(name, setup, drive) {
  const log = { opened: [], reloads: 0, closes: 0, fetches: [], submits: [], prevented: 0, focused: 0 };
  const timers = [];
  let now = 0;
  const listeners = {};
  const element = (extra) =>
    Object.assign(
      {
        hidden: true,
        dataset: {},
        listeners: {},
        addEventListener(type, fn) {
          (this.listeners[type] = this.listeners[type] || []).push(fn);
        },
        querySelector() {
          return null;
        },
        focus() {
          log.focused += 1;
        },
      },
      extra
    );
  const world = setup({ log, element });
  const popup = world.popup === undefined ? { close: () => (log.closes += 1) } : world.popup;
  const responses = world.responses || [];
  const window = {
    open: (...args) => {
      log.opened.push(args);
      return popup;
    },
    location: { reload: () => (log.reloads += 1) },
    setTimeout: (fn, delay) => timers.push({ fn, delay }),
    close: () => (log.closes += 1),
    // Any cross-window messaging would be a defect: fail loudly if the script reaches for it.
    postMessage: () => {
      throw new Error("postMessage used");
    },
  };
  const context = {
    window,
    document: {
      querySelectorAll: (selector) => (selector === "form[data-provider-connect]" ? world.forms : []),
      querySelector: (selector) => world.nodes[selector] || null,
    },
    fetch: (url, options) => {
      log.fetches.push([url, options.credentials, options.cache]);
      const next = responses.shift();
      return next === "fail"
        ? Promise.reject(new Error("offline"))
        : Promise.resolve({ ok: next !== null, json: () => Promise.resolve(next) });
    },
    Date: { now: () => now },
    Number,
    BroadcastChannel: function () {
      throw new Error("BroadcastChannel used");
    },
  };
  vm.createContext(context);
  let error = null;
  try {
    vm.runInContext(source, context);
  } catch (caught) {
    error = String(caught);
  }
  return { name, log, timers, world, window, listeners, error, advance: (ms) => (now += ms) };
}

async function settle() {
  for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

async function run() {
  const out = {};
  const form = (element, display, attempt) =>
    element({
      dataset: { providerConnect: "gmail", status: "/gmail/status", attempt: String(attempt) },
      target: "provider-auth",
      removed: [],
      removeAttribute(name) {
        this.removed.push(name);
        if (name === "target") this.target = "";
      },
      querySelector: (selector) => (selector === 'input[name="display"]' ? display : null),
      submit() {
        this.submitted = { target: this.target, display: display.value };
      },
    });
  const submit = (f, log) => {
    const event = { preventDefault: () => (log.prevented += 1) };
    f.listeners.submit.forEach((fn) => fn(event));
  };

  // 1. The window opens inside the click, waiting is shown, completion reloads once.
  let s = scenario("opens", ({ log, element }) => {
    const display = { value: "popup" };
    const f = form(element, display, 5);
    const panel = element({});
    const blocked = element({});
    return {
      forms: [f],
      f,
      panel,
      blocked,
      nodes: { '[data-auth-wait="gmail"]': panel, '[data-auth-blocked="gmail"]': blocked },
      responses: [
        { attempt: 5, state: "idle" }, // the POST has not reached the server yet
        { attempt: 6, state: "pending" },
        { attempt: 6, state: "idle" },
      ],
    };
  });
  submit(s.world.f, s.log);
  await settle();
  const steps = [];
  for (let i = 0; i < 4 && s.timers.length; i += 1) {
    s.timers.shift().fn();
    await settle();
    steps.push(s.log.reloads);
  }
  out.opens = {
    error: s.error,
    opened: s.log.opened,
    prevented: s.log.prevented,
    waitingShown: s.world.panel.hidden === false,
    blockedShown: s.world.blocked.hidden === false,
    fetches: s.log.fetches,
    reloadsAfterEachPoll: steps,
    reloads: s.log.reloads,
    popupClosedBeforeReload: s.log.closes,
  };

  // 2. A blocked popup explains itself, focuses the explicit continuation and does not poll.
  s = scenario("blocked", ({ log, element }) => {
    const display = { value: "popup" };
    const f = form(element, display, 1);
    const panel = element({});
    const button = element({});
    const blocked = element({ querySelector: (selector) => (selector === "button" ? button : null) });
    return {
      forms: [f],
      f,
      display,
      panel,
      blocked,
      button,
      popup: null,
      nodes: { '[data-auth-wait="gmail"]': panel, '[data-auth-blocked="gmail"]': blocked },
    };
  });
  submit(s.world.f, s.log);
  await settle();
  const before = { prevented: s.log.prevented, focused: s.log.focused, fetches: s.log.fetches.length };
  s.world.button.listeners.click.forEach((fn) => fn());
  out.blocked = {
    error: s.error,
    before,
    blockedShown: s.world.blocked.hidden === false,
    waitingShown: s.world.panel.hidden === false,
    submitted: s.world.f.submitted,
    removed: s.world.f.removed,
    timers: s.timers.length,
  };

  // 3. A same-window form (no target) is left alone.
  s = scenario("same-window", ({ log, element }) => {
    const f = form(element, { value: "window" }, 1);
    f.target = "";
    return { forms: [f], f, nodes: {} };
  });
  submit(s.world.f, s.log);
  out.sameWindow = { error: s.error, opened: s.log.opened.length, prevented: s.log.prevented };

  // 4. A page reloaded mid sign-in resumes watching and leaves once the attempt ends.
  s = scenario("resume", ({ element }) => {
    const resume = element({ dataset: { status: "/ai/status" }, hidden: false });
    return {
      forms: [],
      nodes: { "[data-auth-resume]": resume },
      responses: [{ attempt: 3, state: "pending" }, { attempt: 3, state: "idle" }],
    };
  });
  await settle();
  s.timers.shift().fn();
  await settle();
  out.resume = { error: s.error, fetches: s.log.fetches.map((f) => f[0]), reloads: s.log.reloads };

  // 5. Offline or failing polls never throw or reload, and stop after the timeout.
  s = scenario("failing", ({ element }) => {
    const resume = element({ dataset: { status: "/gmail/status" }, hidden: false });
    return {
      forms: [],
      nodes: { "[data-auth-resume]": resume },
      responses: ["fail", null, "fail"],
    };
  });
  await settle();
  let polls = 0;
  while (s.timers.length && polls < 10) {
    s.advance(300000);
    s.timers.shift().fn();
    await settle();
    polls += 1;
  }
  out.failing = { error: s.error, reloads: s.log.reloads, stopped: s.timers.length === 0 };

  // 6. The result page offers and attempts to close the sign-in window.
  s = scenario("result", ({ element }) => {
    const close = element({});
    return {
      forms: [],
      close,
      nodes: { "[data-auth-result]": element({}), "[data-close-window]": close },
    };
  });
  s.world.close.listeners.click.forEach((fn) => fn());
  const manual = s.log.closes;
  const timer = s.timers.shift();
  timer.fn();
  out.result = {
    error: s.error,
    closeShown: s.world.close.hidden === false,
    manual,
    automatic: s.log.closes - manual,
    delay: timer.delay,
  };
  console.log(JSON.stringify(out));
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
