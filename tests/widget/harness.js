'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SCRIPT_PATH = path.join(__dirname, '..', '..', 'resources', 'AUDAPACK_WIDGET.user.js');
const AUTO_TAB_SESSION_KEY = 'ai_chatbuttons_auto_tab_id_v1';
const AUTO_DRAFT_SESSION_KEY = 'ai_chatbuttons_auto_draft_id_v1';

let uuidCounter = 0;

const counters = {
  rectReads: 0,
  cdp: 0,
  qsa: 0,
  innerTextReads: 0,
  gmGet: 0,
  gmSet: 0
};

function nextUuid() {
  uuidCounter += 1;
  return `00000000-0000-4000-8000-${String(uuidCounter).padStart(12, '0')}`;
}

// ---------------------------------------------------------------------------
// Selector engine (subset of CSS used by AUDAPACK_WIDGET.user.js)
// ---------------------------------------------------------------------------

function parseAttrToken(token) {
  // token looks like [name], [name=value], [name^=value], [name*=value "i"]
  const inner = token.slice(1, -1).trim();
  const opMatch = inner.match(/^([^\s=~^$*|]+)\s*(\^=|\$=|\*=|=|~=|\|=)?\s*(.*)$/);
  if (!opMatch) return null;
  const name = opMatch[1].toLowerCase();
  const op = opMatch[2] || null;
  let rawValue = (opMatch[3] || '').trim();
  let insensitive = false;
  const flagMatch = rawValue.match(/\s*i$/i);
  if (flagMatch) {
    insensitive = true;
    rawValue = rawValue.slice(0, flagMatch.index);
  }
  if (rawValue.startsWith('"') && rawValue.endsWith('"')) rawValue = rawValue.slice(1, -1);
  if (rawValue.startsWith("'") && rawValue.endsWith("'")) rawValue = rawValue.slice(1, -1);
  return { name, op, value: rawValue, insensitive };
}

function matchAttrToken(element, token) {
  const parsed = parseAttrToken(token);
  if (!parsed) return false;
  const attr = element.attributes.get(parsed.name);
  if (attr === undefined || attr === null) return parsed.op === null ? false : false;
  const actual = String(attr);
  if (parsed.op === null) return true;
  const expected = String(parsed.value);
  const a = parsed.insensitive ? actual.toLowerCase() : actual;
  const e = parsed.insensitive ? expected.toLowerCase() : expected;
  switch (parsed.op) {
    case '=': return a === e;
    case '^=': return a.startsWith(e);
    case '$=': return a.endsWith(e);
    case '*=': return a.includes(e);
    case '~=': return a.split(/\s+/).includes(e);
    case '|=': return a === e || a.startsWith(`${e}-`);
    default: return false;
  }
}

function matchCompound(element, compound) {
  const tokens = compound.split(/(?=\.)|(?=#)|(?=\[)/);
  let tag = null;
  for (const token of tokens) {
    if (!token) continue;
    if (token[0] === '#') {
      if (element.attributes.get('id') !== token.slice(1)) return false;
    } else if (token[0] === '.') {
      if (!element.classList.contains(token.slice(1))) return false;
    } else if (token[0] === '[') {
      if (!matchAttrToken(element, token)) return false;
    } else {
      tag = token.toLowerCase();
    }
  }
  if (tag && element.tagName.toLowerCase() !== tag) return false;
  return true;
}

function matchesSelector(element, selector) {
  if (!element) return false;
  const groups = String(selector || '').split(',').map(s => s.trim()).filter(Boolean);
  for (const group of groups) {
    if (matchesComplex(element, group)) return true;
  }
  return false;
}

function splitSelectorParts(selector) {
  const parts = [];
  let current = '';
  let depth = 0;
  let quote = '';
  for (let i = 0; i < selector.length; i += 1) {
    const ch = selector[i];
    if (quote) {
      current += ch;
      if (ch === quote) quote = '';
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      current += ch;
      continue;
    }
    if (ch === '[') depth += 1;
    if (ch === ']') depth -= 1;
    if (/\s/.test(ch) && depth === 0) {
      if (current.trim()) parts.push(current.trim());
      current = '';
      continue;
    }
    current += ch;
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

function matchesComplex(element, group) {
  const parts = splitSelectorParts(group);
  if (!parts.length) return false;
  if (!matchCompound(element, parts[parts.length - 1])) return false;
  if (parts.length === 1) return true;
  const ancestors = allAncestors(element);
  return matchAncestorChain(ancestors, parts.slice(0, -1));
}

function matchAncestorChain(ancestors, parts) {
  if (!parts.length) return true;
  const target = parts[parts.length - 1];
  for (let i = 0; i < ancestors.length; i += 1) {
    if (!matchCompound(ancestors[i], target)) continue;
    if (matchAncestorChain(ancestors.slice(i + 1), parts.slice(0, -1))) return true;
  }
  return false;
}

function allAncestors(element) {
  const out = [];
  let current = element.parentNode;
  while (current) {
    out.push(current);
    current = current.parentNode;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Minimal HTML parsing for innerHTML (panel template only)
// ---------------------------------------------------------------------------

const VOID_TAGS = new Set(['input', 'br', 'img', 'hr', 'meta', 'link', 'wbr', 'col', 'area', 'base', 'embed', 'source', 'track']);

function parseHtml(html) {
  const root = [];
  const stack = [];
  const tokenRe = /<!--[\s\S]*?-->|<(\/)?([a-zA-Z][\w-]*)((?:[^>"']|"[^"]*"|'[^']*')*?)(\/)?>|([^<]+)/g;
  let match;
  while ((match = tokenRe.exec(html)) !== null) {
    if (match[0].startsWith('<!--')) continue;
    if (match[5] !== undefined) {
      const text = match[5];
      if (stack.length) stack[stack.length - 1]._text += text;
      continue;
    }
    const closing = Boolean(match[1]);
    const tag = match[2].toLowerCase();
    const attrPart = match[3] || '';
    const selfClosing = Boolean(match[4]) || VOID_TAGS.has(tag);
    if (closing) {
      while (stack.length) {
        const top = stack.pop();
        if (top.tagName.toLowerCase() === tag) break;
      }
      continue;
    }
    const attrs = {};
    const attrRe = /([a-zA-Z_:][\w:.-]*)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
    let attrMatch;
    while ((attrMatch = attrRe.exec(attrPart)) !== null) {
      const name = attrMatch[1];
      const value = attrMatch[3] !== undefined ? attrMatch[3] : attrMatch[4] !== undefined ? attrMatch[4] : attrMatch[5];
      attrs[name] = value === undefined ? true : value;
    }
    const element = new FakeElement(tag, attrs);
    if (attrs.contenteditable === 'true') element.isContentEditable = true;
    if (attrs.hidden !== undefined) element.hidden = true;
    if (attrs.disabled !== undefined) element.disabled = true;
    if (stack.length) stack[stack.length - 1].appendChild(element);
    else root.push(element);
    if (!selfClosing) stack.push(element);
  }
  return root;
}

// ---------------------------------------------------------------------------
// Fake DOM
// ---------------------------------------------------------------------------

class FakeStyle {
  constructor() { this._props = new Map(); this.cssText = ''; }
  setProperty(name, value) { this._props.set(name, String(value)); }
  getPropertyValue(name) { return this._props.get(name) || ''; }
  removeProperty(name) { this._props.delete(name); }
}

class FakeClassList {
  constructor(element) {
    this._element = element;
  }
  _names() {
    return String(this._element.attributes.get('class') || '').split(/\s+/).filter(Boolean);
  }
  _write(names) {
    this._element.setAttribute('class', names.join(' '));
  }
  contains(name) { return this._names().includes(name); }
  add(...names) {
    const set = new Set(this._names());
    for (const name of names) set.add(name);
    this._write(Array.from(set));
  }
  remove(...names) {
    const set = new Set(this._names());
    for (const name of names) set.delete(name);
    this._write(Array.from(set));
  }
  toggle(name, force) {
    const has = this.contains(name);
    const want = force === undefined ? !has : Boolean(force);
    if (want && !has) this.add(name);
    if (!want && has) this.remove(name);
    return want;
  }
}

class FakeElement {
  constructor(tagName, attrs = {}, text = '') {
    this.tagName = String(tagName).toUpperCase();
    this.attributes = new Map();
    this.children = [];
    this.parentNode = null;
    this._listeners = new Map();
    this._text = '';
    this.hidden = false;
    this.disabled = false;
    this.style = new FakeStyle();
    this.isContentEditable = false;
    this.nodeType = 1;
    this.shadowRoot = null;
    this.dataset = {};
    for (const [key, value] of Object.entries(attrs)) {
      if (value !== undefined && value !== null && value !== false) {
        this.setAttribute(key, value === true ? '' : String(value));
      }
    }
    if (text) this._text = String(text);
    this.classList = new FakeClassList(this);
    const tag = String(tagName).toLowerCase();
    if (tag === 'input' || tag === 'textarea') {
      Object.defineProperty(this, 'value', {
        configurable: true,
        enumerable: true,
        get() { return this._value || ''; },
        set(v) { this._value = String(v == null ? '' : v); }
      });
    }
  }

  get id() { return this.attributes.get('id') || ''; }
  set id(value) { this.setAttribute('id', value); }

  get textContent() {
    if (this.children.length) return this.children.map(c => c.textContent).join('');
    return this._text;
  }
  set textContent(value) {
    this.children = [];
    this._text = String(value == null ? '' : value);
  }

  get innerText() { counters.innerTextReads += 1; return this.textContent; }
  set innerText(value) { this.textContent = value; }

  get innerHTML() { return ''; }
  set innerHTML(value) {
    this.children = parseHtml(String(value));
    for (const child of this.children) child.parentNode = this;
    this._text = '';
  }

  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }

  get isConnected() {
    let current = this;
    while (current.parentNode) current = current.parentNode;
    return current === this._rootNode;
  }
  get _rootNode() {
    let current = this;
    while (current.parentNode) current = current.parentNode;
    return current;
  }

  setAttribute(name, value) {
    const lower = String(name).toLowerCase();
    this.attributes.set(lower, String(value));
    if (lower === 'class') this.classList = new FakeClassList(this);
    if (lower === 'id') {
      Object.defineProperty(this, 'id', { configurable: true, enumerable: true, get: () => String(value), set: v => this.setAttribute('id', v) });
    }
  }
  getAttribute(name) {
    return this.attributes.get(String(name).toLowerCase()) ?? null;
  }
  removeAttribute(name) {
    this.attributes.delete(String(name).toLowerCase());
  }
  hasAttribute(name) {
    return this.attributes.has(String(name).toLowerCase());
  }
  toggleAttribute(name, force) {
    const has = this.hasAttribute(name);
    const want = force === undefined ? !has : Boolean(force);
    if (want) this.setAttribute(name, '');
    else this.removeAttribute(name);
    return want;
  }

  appendChild(child) {
    if (!(child instanceof FakeElement)) return child;
    child.remove();
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  append(...children) {
    for (const child of children) this.appendChild(child);
  }
  prepend(child) {
    if (!(child instanceof FakeElement)) return child;
    child.remove();
    child.parentNode = this;
    this.children.unshift(child);
    return child;
  }
  insertBefore(child, reference) {
    if (!(child instanceof FakeElement)) return child;
    const index = reference ? this.children.indexOf(reference) : -1;
    child.remove();
    child.parentNode = this;
    if (index < 0) this.children.push(child);
    else this.children.splice(index, 0, child);
    return child;
  }
  remove() {
    if (this.parentNode) {
      const index = this.parentNode.children.indexOf(this);
      if (index >= 0) this.parentNode.children.splice(index, 1);
      this.parentNode = null;
    }
  }
  replaceChild(newChild, oldChild) {
    const index = this.children.indexOf(oldChild);
    if (index < 0) return oldChild;
    newChild.remove();
    newChild.parentNode = this;
    this.children.splice(index, 1, newChild);
    return oldChild;
  }
  contains(other) {
    let current = other;
    while (current) {
      if (current === this) return true;
      current = current.parentNode;
    }
    return false;
  }
  getBoundingClientRect() {
    counters.rectReads += 1;
    if (this.hidden || this.getAttribute('hidden') !== null) return { width: 0, height: 0, top: 0, left: 0, bottom: 0, right: 0 };
    return { width: 1, height: 1, top: 0, left: 0, bottom: 1, right: 1 };
  }
  focus() {}
  blur() {}
  matches(selector) { return matchesSelector(this, selector); }
  closest(selector) {
    let current = this;
    while (current) {
      if (matchesSelector(current, selector)) return current;
      current = current.parentNode;
    }
    return null;
  }
  querySelectorAll(selector) {
    counters.qsa += 1;
    const out = [];
    const walk = node => {
      for (const child of node.children) {
        if (matchesSelector(child, selector)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  querySelector(selector) {
    const out = this.querySelectorAll(selector);
    return out[0] || null;
  }
  compareDocumentPosition(other) {
    counters.cdp += 1;
    const order = this._documentOrder();
    const a = order.get(this);
    const b = order.get(other);
    if (a === undefined || b === undefined) return 0;
    if (a < b) return 4; // DOCUMENT_POSITION_FOLLOWING
    if (a > b) return 2; // DOCUMENT_POSITION_PRECEDING
    return 0;
  }
  _documentOrder() {
    let root = this;
    while (root.parentNode) root = root.parentNode;
    const order = new Map();
    let index = 0;
    const walk = node => {
      order.set(node, index);
      index += 1;
      for (const child of node.children) walk(child);
    };
    walk(root);
    return order;
  }
  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type).add(fn);
  }
  removeEventListener(type, fn) {
    this._listeners.get(type)?.delete(fn);
  }
  dispatchEvent(event) {
    event.target = event.target || this;
    event.currentTarget = this;
    const listeners = this._listeners.get(event.type);
    if (listeners) for (const fn of Array.from(listeners)) fn.call(this, event);
    return !event.defaultPrevented;
  }
  click() {
    this._clicked = true;
    this.dispatchEvent(new FakeEvent('click', { bubbles: true }));
  }
}

class FakeEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = Boolean(init.bubbles);
    this.cancelable = Boolean(init.cancelable);
    this.defaultPrevented = false;
    this.target = null;
    this.data = init.data ?? null;
    this.inputType = init.inputType ?? '';
    this.checked = init.checked ?? false;
    this.value = init.value ?? '';
    this.clientX = init.clientX ?? 0;
    this.clientY = init.clientY ?? 0;
    this.pointerId = init.pointerId ?? 0;
    this.isPrimary = init.isPrimary ?? false;
    this.button = init.button ?? 0;
  }
  preventDefault() { this.defaultPrevented = true; }
}

class FakeInputEvent extends FakeEvent {}

class FakeDocument {
  constructor(harness) {
    this._harness = harness;
    this.documentElement = new FakeElement('html');
    this.body = new FakeElement('body');
    this.head = new FakeElement('head');
    this.documentElement.appendChild(this.head);
    this.documentElement.appendChild(this.body);
    this._main = new FakeElement('main');
    this.body.appendChild(this._main);
  }
  querySelectorAll(selector) {
    counters.qsa += 1;
    return this.documentElement.querySelectorAll(selector);
  }
  querySelector(selector) {
    return this.documentElement.querySelector(selector);
  }
  getElementById(id) {
    return this.documentElement.querySelector(`#${id}`);
  }
  createElement(tag) {
    return new FakeElement(tag);
  }
  createTextNode(text) {
    const node = new FakeElement('#text');
    node.nodeType = 3;
    node._text = String(text);
    return node;
  }
  createTreeWalker() {
    return { nextNode: () => null };
  }
  createRange() {
    return { selectNodeContents() {}, collapse() {}, getBoundingClientRect: () => ({ width: 0, height: 0 }) };
  }
  execCommand() { return false; }
  addEventListener() {}
  removeEventListener() {}
}

class FakeMutationObserver {
  constructor(callback) {
    this.callback = callback;
    this.root = null;
    this.options = null;
    this.connected = false;
    this._harness = null;
  }
  observe(root, options) {
    this.root = root;
    this.options = options || null;
    this.connected = true;
    if (this._harness) this._harness._observers.add(this);
  }
  disconnect() {
    this.connected = false;
    if (this._harness) this._harness._observers.delete(this);
  }
  takeRecords() { return []; }
}

// ---------------------------------------------------------------------------
// Fake timers
// ---------------------------------------------------------------------------

class FakeTimers {
  constructor() {
    this._now = 0;
    this._nextId = 1;
    this._timers = new Map();
  }
  get now() { return this._now; }
  setTimeout(fn, ms = 0) {
    const id = this._nextId++;
    this._timers.set(id, { id, type: 'timeout', due: this._now + Math.max(0, Number(ms) || 0), fn });
    return id;
  }
  setInterval(fn, ms = 0) {
    const id = this._nextId++;
    this._timers.set(id, { id, type: 'interval', due: this._now + Math.max(0, Number(ms) || 0), interval: Math.max(0, Number(ms) || 0), fn });
    return id;
  }
  clearTimeout(id) { this._timers.delete(id); }
  clearInterval(id) { this._timers.delete(id); }
  pending() {
    return Array.from(this._timers.values()).map(t => Math.max(0, t.due - this._now));
  }
  advance(ms) {
    const target = this._now + ms;
    let guard = 0;
    while (guard < 100000) {
      guard += 1;
      let next = null;
      let nextDue = Infinity;
      for (const timer of this._timers.values()) {
        if (timer.due < nextDue) {
          nextDue = timer.due;
          next = timer;
        }
      }
      if (!next || nextDue > target) break;
      this._now = nextDue;
      if (next.type === 'timeout') this._timers.delete(next.id);
      else {
        next.due = this._now + next.interval;
        this._timers.set(next.id, next);
      }
      next.fn();
    }
    this._now = target;
  }
  flush() {
    const pending = this.pending();
    if (!pending.length) return;
    this.advance(Math.max(...pending));
  }
  sleep(ms) {
    return new Promise(resolve => {
      const id = this.setTimeout(resolve, ms);
      this._sleepIds = this._sleepIds || new Set();
      this._sleepIds.add(id);
    });
  }
}

// ---------------------------------------------------------------------------
// Harness factory
// ---------------------------------------------------------------------------

function createHarness() {
  Object.assign(counters, { rectReads: 0, cdp: 0, qsa: 0, innerTextReads: 0, gmGet: 0, gmSet: 0 });
  const timers = new FakeTimers();
  const gmStore = new Map();
  const sessionStore = new Map();
  const localStore = new Map();
  const observers = new Set();

  const document = new FakeDocument(null);
  const location = { hostname: 'chatgpt.com', host: 'chatgpt.com', href: 'https://chatgpt.com/c/abc123', pathname: '/c/abc123', search: '', protocol: 'https:' };

  const windowObj = {
    _listeners: new Map(),
    addEventListener(type, fn) {
      if (!this._listeners.has(type)) this._listeners.set(type, new Set());
      this._listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      this._listeners.get(type)?.delete(fn);
    },
    dispatchEvent(event) {
      event.target = event.target || this;
      event.currentTarget = this;
      const listeners = this._listeners.get(event.type);
      if (listeners) for (const fn of Array.from(listeners)) fn.call(this, event);
      return !event.defaultPrevented;
    },
    innerWidth: 800,
    innerHeight: 600,
    devicePixelRatio: 1,
    visualViewport: undefined,
    getSelection: () => null,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
    requestAnimationFrame: fn => timers.setTimeout(fn, 16),
    cancelAnimationFrame: id => timers.clearTimeout(id),
    setTimeout: (...args) => timers.setTimeout(...args),
    clearTimeout: id => timers.clearTimeout(id),
    setInterval: (...args) => timers.setInterval(...args),
    clearInterval: id => timers.clearInterval(id),
    alert() {},
    confirm: () => false,
    prompt: () => null,
    trustedTypes: undefined,
    MutationObserver: function (callback) {
      const observer = new FakeMutationObserver(callback);
      observer._harness = harness;
      return observer;
    }
  };

  const bareTimers = {
    setTimeout: (...args) => timers.setTimeout(...args),
    clearTimeout: id => timers.clearTimeout(id),
    setInterval: (...args) => timers.setInterval(...args),
    clearInterval: id => timers.clearInterval(id),
    requestAnimationFrame: fn => timers.setTimeout(fn, 16),
    cancelAnimationFrame: id => timers.clearTimeout(id)
  };

  const harness = { timers, gmStore, sessionStore, localStore, _observers: observers, api: null, dom: document, location, counters, window: windowObj };
  document._harness = harness;

  const sandbox = {
    console,
    document,
    window: windowObj,
    location,
    navigator: { userAgent: 'harness', platform: 'test' },
    performance: { now: () => Date.now() },
    crypto: { randomUUID: nextUuid, getRandomValues: () => ({}) },
    Math,
    Date,
    JSON,
    Object,
    Array,
    String,
    Number,
    Boolean,
    Promise,
    Set,
    Map,
    Symbol,
    Reflect,
    Error,
    TypeError,
    RegExp,
    URL,
    URLSearchParams,
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3, DOCUMENT_POSITION_FOLLOWING: 4, DOCUMENT_POSITION_PRECEDING: 2, DOCUMENT_POSITION_CONTAINS: 8, DOCUMENT_POSITION_CONTAINED_BY: 16 },
    NodeFilter: { SHOW_ELEMENT: 1 },
    Event: FakeEvent,
    InputEvent: FakeInputEvent,
    MutationObserver: function (callback) {
      const observer = new FakeMutationObserver(callback);
      observer._harness = harness;
      return observer;
    },
    GM_getValue: (key, def) => { counters.gmGet += 1; return gmStore.has(key) ? gmStore.get(key) : def; },
    GM_setValue: (key, value) => { counters.gmSet += 1; gmStore.set(key, value); },
    GM_deleteValue: key => { gmStore.delete(key); },
    GM_addStyle: () => {},
    GM_registerMenuCommand: () => 0,
    GM_unregisterMenuCommand: () => {},
    GM_openInTab: () => {},
    GM_notification: () => {},
    sessionStorage: {
      getItem: key => (sessionStore.has(key) ? sessionStore.get(key) : null),
      setItem: (key, value) => { sessionStore.set(key, String(value)); },
      removeItem: key => { sessionStore.delete(key); }
    },
    localStorage: {
      getItem: key => (localStore.has(key) ? localStore.get(key) : null),
      setItem: (key, value) => { localStore.set(key, String(value)); },
      removeItem: key => { localStore.delete(key); }
    },
    ...bareTimers,
    __ACB_ENABLE_TEST_HOOK__: true,
    globalThis: null
  };

  sandbox.globalThis = sandbox;

  harness._sleepIds = new Set();
  timers.sleep = ms => new Promise(resolve => {
    const id = timers.setTimeout(resolve, ms);
    harness._sleepIds.add(id);
  });

  harness.load = () => {
    const source = fs.readFileSync(SCRIPT_PATH, 'utf8');
    vm.createContext(sandbox);
    try {
      vm.runInContext(source, sandbox, { filename: 'AUDAPACK_WIDGET.user.js' });
    } catch (error) {
      harness.loadError = error;
    }
    harness.api = sandbox.__ACB_TEST__ || null;
    return harness.api;
  };

harness.settle = async () => {
    let guard = 0;
    while (guard < 100) {
      guard += 1;
      await new Promise(resolve => setImmediate(resolve));
      const near = timers.pending().filter(due => due <= 5000);
      if (!near.length) break;
      timers.advance(Math.max(...near));
    }
  };

  harness.advance = ms => timers.advance(ms);

  harness.mutate = root => {
    for (const observer of Array.from(observers)) {
      if (observer.connected && observer.root === root) observer.callback([], observer);
    }
  };

  harness.el = (tag, attrs, text) => new FakeElement(tag, attrs, text);

  return harness;
}

module.exports = { createHarness, FakeElement, FakeEvent, matchesSelector, parseAttrToken, matchCompound };