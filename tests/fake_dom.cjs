// Крошечный поддельный DOM для харнессов страницы.
//
// Настоящего DOM в проекте нет (jsdom не поставлен), а проверять код
// страницы надо. Здесь ровно то, на что он опирается: узлы, классы,
// набор селекторов, перенос между родителями. Встретив незнакомый
// селектор, стуб ругается, а не возвращает молча null — иначе проверка
// проходит на пустоте.
const assert = require('assert');

const SEL = /^([a-z]*)((?:\.[-\w]+)*)(?:\[([-\w]+)(?:="([^"]*)")?\])?$/;

function parseSel(sel) {
  const m = SEL.exec(sel.trim());
  assert.ok(m, 'стуб не знает такого селектора: ' + sel);
  return {
    tag: m[1] || '',
    classes: (m[2] || '').split('.').filter(Boolean),
    attr: m[3] || '',
    value: m[4],
  };
}

function el(tag, opts = {}) {
  const node = {
    tag,
    dataset: opts.dataset || {},
    hidden: false,
    value: opts.value !== undefined ? opts.value : '',
    textContent: opts.textContent || '',
    children: [],
    parentNode: null,
    _classes: new Set(opts.classes || []),
    classList: {
      add: (c) => node._classes.add(c),
      remove: (c) => node._classes.delete(c),
      contains: (c) => node._classes.has(c),
      toggle: (c, on) => (on === undefined ? (node._classes.has(c) ? node._classes.delete(c) : node._classes.add(c))
                                           : (on ? node._classes.add(c) : node._classes.delete(c))),
    },
    matches(sel) {
      const s = parseSel(sel);
      if (s.tag && s.tag !== node.tag) return false;
      if (!s.classes.every((c) => node._classes.has(c))) return false;
      if (s.attr) {
        const key = s.attr.replace(/^data-/, '').replace(/-(\w)/g, (_, c) => c.toUpperCase());
        const has = s.attr.startsWith('data-') ? node.dataset[key] !== undefined : node[s.attr] !== undefined;
        if (!has) return false;
        if (s.value !== undefined) {
          const got = s.attr.startsWith('data-') ? node.dataset[key] : node[s.attr];
          if (String(got) !== s.value) return false;
        }
      }
      return true;
    },
    querySelectorAll(sel) {
      const out = [];
      (function walk(n) {
        n.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); });
      })(node);
      return out;
    },
    querySelector(sel) { return node.querySelectorAll(sel)[0] || null; },
    appendChild(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = node;
      node.children.push(child);
      return child;
    },
    removeChild(child) {
      node.children = node.children.filter((c) => c !== child);
      child.parentNode = null;
      return child;
    },
    insertBefore(child, ref) {
      if (child.parentNode) child.parentNode.removeChild(child);
      const i = node.children.indexOf(ref);
      node.children.splice(i === -1 ? node.children.length : i, 0, child);
      child.parentNode = node;
      return child;
    },
    remove() { if (node.parentNode) node.parentNode.removeChild(node); },
    addEventListener(name, fn) { (node._on = node._on || {})[name] = (node._on[name] || []).concat(fn); },
    fire(name, e) { ((node._on || {})[name] || []).forEach((fn) => fn(e)); },
    closest(sel) {
      let n = node;
      while (n) { if (n.matches && n.matches(sel)) return n; n = n.parentNode; }
      return null;
    },
    contains(other) {
      let n = other;
      while (n) { if (n === node) return true; n = n.parentNode; }
      return false;
    },
  };
  // `className = '...'` — обычный способ задать класс на созданном узле;
  // без него classList остаётся пустым, и узел не находится ни одним
  // селектором.
  Object.defineProperty(node, 'className', {
    get: () => [...node._classes].join(' '),
    set: (v) => { node._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  (opts.children || []).forEach((c) => node.appendChild(c));
  return node;
}

module.exports = { el, parseSel };
