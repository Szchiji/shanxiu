'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.className = '';
    this.disabled = false;
    this.onclick = null;
    this.href = '';
    this.target = '';
    this.rel = '';
    this.type = '';
    this.id = '';
    this._text = '';
    this.parentNode = null;
  }
  set textContent(value) {
    this._text = String(value == null ? '' : value);
    this.children = [];
  }
  get textContent() {
    if (this.children.length) {
      return this.children.map((child) => child.textContent).join('');
    }
    return this._text;
  }
  set innerHTML(_value) {
    throw new Error('user content must not be assigned to innerHTML');
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  replaceChildren() {
    this.children = [];
    this._text = '';
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'id') this.id = String(value);
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  querySelectorAll(selector) {
    const matches = [];
    const visit = (node) => {
      if (node.matches && node.matches(selector)) matches.push(node);
      (node.children || []).forEach(visit);
    };
    this.children.forEach(visit);
    return matches;
  }
  matches(selector) {
    if (selector.startsWith('.')) {
      return this.className.split(/\s+/).includes(selector.slice(1));
    }
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.includes('[')) {
      const name = selector.slice(selector.indexOf('[') + 1, selector.indexOf(']')).replace(/['"]/g, '');
      const [key, raw] = name.split('=');
      if (raw == null) return Object.prototype.hasOwnProperty.call(this.attributes, key) || (key === 'data-report-id' && this.dataset.reportId != null);
      return String(this.attributes[key] || '') === raw;
    }
    return this.tagName === selector.toUpperCase();
  }
}

function createDocument() {
  const app = new Element('div');
  app.id = 'app';
  return {
    app,
    document: {
      createElement(tag) { return new Element(tag); },
    },
  };
}

function loadViewApi() {
  const source = fs.readFileSync(
    path.resolve(__dirname, '../../app/modules/report/templates/report_webapp_view.js'),
    'utf8',
  );
  const sandbox = { window: {}, globalThis: {}, console };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  if (!sandbox.ReportWebAppView) throw new Error('ReportWebAppView was not exported');
  return sandbox.ReportWebAppView;
}

function assert(condition, message) {
  if (!condition) throw new Error(message || 'assertion failed');
}

function find(node, predicate, acc) {
  const out = acc || [];
  if (predicate(node)) out.push(node);
  (node.children || []).forEach((child) => find(child, predicate, out));
  return out;
}

const api = loadViewApi();
const boot = {
  page: 2,
  sort: 'oldest',
  total: 5,
  has_more: true,
  timezone: '北京时间',
  target_user_id: 42,
  target_contact: { name: '猫', user_id: 42, url: 'https://t.me/mao' },
  reports: [
    { id: 17, title: '小猫老师', date: '11-16 14:48', summary: '测\n今日顺利' },
    { id: 16, title: '报告 #16', date: '11-16 13:02', summary: '<img src=x onerror=alert(1)>' },
  ],
};

const { app, document } = createDocument();
const sandboxDoc = { document, window: { document } };
// Recreate API against this document by injecting document into vm? The helpers
// close over the global document at call time via the identifier `document`.
// Load them with the mock document in context instead.
function loadWithDocument(doc) {
  const source = fs.readFileSync(
    path.resolve(__dirname, '../../app/modules/report/templates/report_webapp_view.js'),
    'utf8',
  );
  const sandbox = { document: doc, window: { document: doc }, console };
  sandbox.globalThis = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return sandbox.ReportWebAppView;
}

const view = loadWithDocument(document);
let sortRequested = null;
let pageRequested = null;
let opened = null;
view.renderView(app, boot, {
  onSort: (sort) => { sortRequested = sort; },
  loadPage: (page, sort) => { pageRequested = [page, sort]; },
  openDetail: (id) => { opened = id; },
});

const title = find(app, (node) => node.className === 'report-title')[0];
assert(title && title.textContent === '小猫老师', 'title should prefer recognizable name');
const summary = find(app, (node) => node.className === 'report-summary')[0];
assert(summary && summary.textContent.includes('测'), 'summary should come from other answers');
assert(summary.style.whiteSpace === 'pre-wrap', 'summary keeps newlines');
const xssSummary = find(app, (node) => node.className === 'report-summary')[1];
assert(xssSummary.textContent === '<img src=x onerror=alert(1)>', 'summary is text, not HTML');
assert(!find(xssSummary, (node) => node.tagName === 'IMG').length, 'HTML in summary must not create elements');
const ids = find(app, (node) => node.className === 'report-id').map((node) => node.textContent);
assert(ids[0] === '#17', 'report id is secondary text');
const contact = find(app, (node) => node.className === 'contact-name')[0];
assert(contact.tagName === 'A' && contact.textContent === '猫' && contact.href === 'https://t.me/mao', 'clickable compact contact');
assert(!app.textContent.includes('投稿'), 'contributor is not shown on the public view');

find(app, (node) => node.dataset && node.dataset.sort === 'newest')[0].onclick();
assert(sortRequested === 'newest', 'sort toggle requests newest and should start at page 1 via hook');
find(app, (node) => node.id === 'nextPage')[0].onclick();
assert(pageRequested[0] === 3 && pageRequested[1] === 'oldest', 'pagination keeps current sort');
find(app, (node) => node.dataset.reportId === '17')[0].onclick();
assert(opened === 17, 'list item opens the selected report');

const longWord = 'A'.repeat(180) + '\n第二行';
let backCalled = false;
view.renderDetail(app, boot, {
  id: 17,
  title: '小猫老师',
  date: '2026-11-16 14:48（北京时间）',
  channel_link: 'https://t.me/reports_test/42',
  answers: [
    { question: '所在地区', answer: '杭州' },
    { question: '情况说明', answer: longWord + '<script>alert(1)</script>' },
  ],
}, { back: () => { backCalled = true; } });

const answers = find(app, (node) => node.className === 'qa-answer');
assert(answers[1].textContent.includes('<script>alert(1)</script>'), 'script text stays as text');
assert(!find(answers[1], (node) => node.tagName === 'SCRIPT').length, 'script is not executed or created');
assert(answers[1].style.whiteSpace === 'pre-wrap', 'long answers preserve newlines');
assert(answers[1].style.overflowWrap === 'anywhere', 'long answers wrap');
assert(answers[1].textContent.includes('第二行'), 'newline content remains');
const channel = find(app, (node) => node.className === 'channel-link')[0];
assert(channel && channel.href === 'https://t.me/reports_test/42', 'channel link is easy to target');
find(app, (node) => node.id === 'backToReports')[0].onclick();
assert(backCalled, 'back returns to the preserved list state via hook');

console.log('ok');
