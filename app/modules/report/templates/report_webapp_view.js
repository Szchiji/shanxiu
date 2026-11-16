(function (root) {
  'use strict';

  function textEl(tag, text, className) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    el.textContent = text == null ? '' : String(text);
    return el;
  }

  function renderContact(parent, contact, fallbackId) {
    var wrap = document.createElement('p');
    wrap.className = 'contact-line';
    wrap.appendChild(textEl('span', '联系人', 'contact-label'));
    var name = (contact && contact.name) || ('用户' + (fallbackId || ''));
    var url = contact && contact.url;
    var uid = contact && contact.user_id != null ? contact.user_id : fallbackId;
    if (url) {
      var link = document.createElement('a');
      link.className = 'contact-name';
      link.href = String(url);
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = name;
      wrap.appendChild(link);
    } else {
      wrap.appendChild(textEl('span', name, 'contact-name'));
    }
    if (uid != null && uid !== '') {
      wrap.appendChild(textEl('span', String(uid), 'contact-id'));
    }
    parent.appendChild(wrap);
    return wrap;
  }

  function renderSortBar(parent, boot, onSort) {
    var bar = document.createElement('div');
    bar.className = 'sort-bar';
    var current = (boot && boot.sort) === 'oldest' ? 'oldest' : 'newest';
    [['newest', '最新'], ['oldest', '最早']].forEach(function (pair) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'sort-btn' + (current === pair[0] ? ' is-active' : '');
      btn.dataset.sort = pair[0];
      btn.textContent = pair[1];
      btn.onclick = function () {
        if (typeof onSort === 'function') onSort(pair[0]);
      };
      bar.appendChild(btn);
    });
    parent.appendChild(bar);
    return bar;
  }

  function renderView(app, boot, hooks) {
    hooks = hooks || {};
    app.replaceChildren();
    var header = document.createElement('div');
    header.className = 'view-header';
    header.appendChild(textEl('h5', '查看报告', 'view-title'));
    renderContact(header, boot.target_contact, boot.target_user_id);
    var meta = document.createElement('p');
    meta.className = 'muted view-meta';
    var total = boot.total == null ? 0 : boot.total;
    var page = boot.page == null ? 1 : boot.page;
    var tz = boot.timezone || '北京时间';
    meta.textContent = '共 ' + total + ' 篇 · 第 ' + page + ' 页 · ' + tz;
    header.appendChild(meta);
    renderSortBar(header, boot, hooks.onSort);
    app.appendChild(header);

    var items = boot.reports || [];
    if (!items.length) {
      var empty = document.createElement('div');
      empty.className = 'card empty-state';
      empty.appendChild(textEl('p', '没有已通过的报告。', 'mb-0'));
      app.appendChild(empty);
    } else {
      var list = document.createElement('div');
      list.className = 'report-list';
      items.forEach(function (report) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'card report-card';
        button.dataset.reportId = String(report.id);
        button.appendChild(textEl('div', report.title || ('报告 #' + report.id), 'report-title'));
        var line = document.createElement('div');
        line.className = 'report-meta';
        line.appendChild(textEl('span', report.date || '', 'report-date'));
        line.appendChild(textEl('span', '#' + report.id, 'report-id'));
        button.appendChild(line);
        if (report.summary) {
          var summary = textEl('div', report.summary, 'report-summary');
          summary.style.whiteSpace = 'pre-wrap';
          summary.style.overflowWrap = 'anywhere';
          summary.style.wordBreak = 'break-word';
          button.appendChild(summary);
        }
        button.onclick = function () {
          if (typeof hooks.openDetail === 'function') hooks.openDetail(report.id);
        };
        list.appendChild(button);
      });
      app.appendChild(list);
    }

    var pages = document.createElement('div');
    pages.className = 'pager';
    var prev = document.createElement('button');
    prev.type = 'button';
    prev.id = 'prevPage';
    prev.className = 'btn btn-outline-secondary pager-btn';
    prev.textContent = '上一页';
    prev.disabled = !(boot.page > 1);
    prev.onclick = function () {
      if (!prev.disabled && typeof hooks.loadPage === 'function') {
        hooks.loadPage(boot.page - 1, boot.sort);
      }
    };
    var next = document.createElement('button');
    next.type = 'button';
    next.id = 'nextPage';
    next.className = 'btn btn-outline-secondary pager-btn';
    next.textContent = '下一页';
    next.disabled = !boot.has_more;
    next.onclick = function () {
      if (!next.disabled && typeof hooks.loadPage === 'function') {
        hooks.loadPage(boot.page + 1, boot.sort);
      }
    };
    pages.appendChild(prev);
    pages.appendChild(next);
    if (boot.page > 1 || boot.has_more) app.appendChild(pages);
  }

  function renderDetail(app, boot, report, hooks) {
    hooks = hooks || {};
    app.replaceChildren();
    var back = document.createElement('button');
    back.type = 'button';
    back.id = 'backToReports';
    back.className = 'btn btn-outline-secondary back-btn';
    back.textContent = '← 返回列表（第 ' + (boot && boot.page ? boot.page : 1) + ' 页）';
    back.onclick = function () {
      if (typeof hooks.back === 'function') hooks.back();
    };
    app.appendChild(back);

    var card = document.createElement('div');
    card.className = 'card detail-card';
    card.appendChild(textEl('h5', (report && report.title) || ('报告 #' + ((report && report.id) || '')), 'detail-title'));
    var meta = document.createElement('div');
    meta.className = 'detail-meta';
    meta.appendChild(textEl('span', (report && report.date) || '', 'detail-date'));
    if (report && report.id != null) meta.appendChild(textEl('span', '#' + report.id, 'report-id'));
    card.appendChild(meta);

    var qa = document.createElement('div');
    qa.className = 'qa-list';
    (report && report.answers ? report.answers : []).forEach(function (item) {
      var block = document.createElement('section');
      block.className = 'qa-item';
      block.appendChild(textEl('div', item.question || '', 'qa-question'));
      var answer = textEl('div', item.answer || '', 'qa-answer');
      answer.style.whiteSpace = 'pre-wrap';
      answer.style.overflowWrap = 'anywhere';
      answer.style.wordBreak = 'break-word';
      block.appendChild(answer);
      qa.appendChild(block);
    });
    card.appendChild(qa);
    if (report && report.channel_link) {
      var channel = document.createElement('a');
      channel.className = 'channel-link';
      channel.href = String(report.channel_link);
      channel.target = '_blank';
      channel.rel = 'noopener noreferrer';
      channel.textContent = '查看频道原文';
      card.appendChild(channel);
    }
    app.appendChild(card);
  }

  function renderDetailError(app, message, hooks) {
    hooks = hooks || {};
    app.replaceChildren();
    var back = document.createElement('button');
    back.type = 'button';
    back.id = 'backToReports';
    back.className = 'btn btn-outline-secondary back-btn';
    back.textContent = '← 返回列表';
    back.onclick = function () {
      if (typeof hooks.back === 'function') hooks.back();
    };
    app.appendChild(back);
    app.appendChild(textEl('p', message || '网络错误，请重试', 'error-text'));
  }

  function renderLoading(app, message) {
    app.replaceChildren();
    app.appendChild(textEl('div', message || '正在加载…', 'p-3 muted text-center'));
  }

  root.ReportWebAppView = {
    renderContact: renderContact,
    renderView: renderView,
    renderDetail: renderDetail,
    renderDetailError: renderDetailError,
    renderLoading: renderLoading,
  };
})(typeof window !== 'undefined' ? window : globalThis);
