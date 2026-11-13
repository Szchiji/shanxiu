/* Shared Telegram rich-text helpers for admin contenteditable editors.
 * Exposes: wrapTelegramFormat, clearTelegramFormats, htmlToTemplate, templateToHtml,
 * getActiveRichEditor.
 */
(function (global) {
    'use strict';

    var BLOCK_TAGS = { pre: true, blockquote: true };
    var FORMAT_TAGS = { code: true, pre: true, 'tg-spoiler': true, blockquote: true };
    var PLACEHOLDER_RE = /^\u0000PH(\d+)\u0000$/;

    function getActiveRichEditor() {
        var sel = global.getSelection && global.getSelection();
        if (sel && sel.rangeCount) {
            var node = sel.anchorNode;
            var el = node && (node.nodeType === 1 ? node : node.parentElement);
            if (el && el.closest) {
                var found = el.closest('[contenteditable="true"]');
                if (found) return found;
            }
        }
        return (global.document && (document.getElementById('editor') || document.getElementById('richEditor'))) || null;
    }

    function isFormatTag(el, tagName) {
        if (!el || el.nodeType !== 1) return false;
        var name = el.tagName.toLowerCase();
        tagName = tagName.toLowerCase();
        if (name === tagName) return true;
        if (tagName === 'tg-spoiler' && name === 'span' && el.classList && el.classList.contains('tg-spoiler')) {
            return true;
        }
        return false;
    }

    function findFormatAncestor(node, tagName, editor) {
        var el = node && (node.nodeType === 1 ? node : node.parentElement);
        while (el && el !== editor && el !== document.body) {
            if (isFormatTag(el, tagName)) return el;
            el = el.parentElement;
        }
        return null;
    }

    function rangeIntersectsNode(range, node) {
        var nr = document.createRange();
        try {
            nr.selectNode(node);
        } catch (e) {
            nr.selectNodeContents(node);
        }
        return range.compareBoundaryPoints(Range.END_TO_START, nr) < 0 &&
            range.compareBoundaryPoints(Range.START_TO_END, nr) > 0;
    }

    function unwrapElement(el) {
        var parent = el.parentNode;
        if (!parent) return;
        while (el.firstChild) {
            parent.insertBefore(el.firstChild, el);
        }
        parent.removeChild(el);
        if (parent.normalize) parent.normalize();
    }

    function collectIntersectingFormats(range, tagName, editor) {
        var found = [];
        var startAnc = findFormatAncestor(range.startContainer, tagName, editor);
        var endAnc = findFormatAncestor(range.endContainer, tagName, editor);
        if (startAnc) found.push(startAnc);
        if (endAnc && found.indexOf(endAnc) === -1) found.push(endAnc);
        var root = range.commonAncestorContainer;
        if (root.nodeType === 3) root = root.parentNode;
        if (!root) return found;
        if (root.nodeType === 1 && isFormatTag(root, tagName) && found.indexOf(root) === -1) {
            found.push(root);
        }
        if (root.querySelectorAll) {
            var nodes = root.querySelectorAll(tagName === 'tg-spoiler' ? 'tg-spoiler, span.tg-spoiler' : tagName);
            for (var i = 0; i < nodes.length; i++) {
                if (rangeIntersectsNode(range, nodes[i]) && found.indexOf(nodes[i]) === -1) {
                    found.push(nodes[i]);
                }
            }
        }
        return found;
    }

    function placeCaretInside(el) {
        var sel = global.getSelection();
        if (!sel) return;
        var nr = document.createRange();
        if (el.firstChild && el.firstChild.nodeType === 3) {
            var offset = el.firstChild.textContent === '\u200B' ? 1 : el.firstChild.textContent.length;
            nr.setStart(el.firstChild, offset);
        } else {
            nr.selectNodeContents(el);
        }
        nr.collapse(true);
        sel.removeAllRanges();
        sel.addRange(nr);
    }

    function wrapTelegramFormat(tagName, editor) {
        tagName = String(tagName || '').toLowerCase();
        if (!FORMAT_TAGS[tagName]) return;
        editor = editor || getActiveRichEditor();
        if (!editor) return;
        editor.focus();
        var sel = global.getSelection();
        if (!sel) return;
        if (!sel.rangeCount) {
            var fallback = document.createRange();
            fallback.selectNodeContents(editor);
            fallback.collapse(false);
            sel.addRange(fallback);
        }
        var range = sel.getRangeAt(0);
        if (!editor.contains(range.commonAncestorContainer) && range.commonAncestorContainer !== editor) {
            range = document.createRange();
            range.selectNodeContents(editor);
            range.collapse(false);
            sel.removeAllRanges();
            sel.addRange(range);
        }

        var startAnc = findFormatAncestor(range.startContainer, tagName, editor);
        var endAnc = findFormatAncestor(range.endContainer, tagName, editor);
        if (startAnc && startAnc === endAnc) {
            unwrapElement(startAnc);
            return;
        }

        var existing = collectIntersectingFormats(range, tagName, editor);
        if (existing.length) {
            for (var i = existing.length - 1; i >= 0; i--) unwrapElement(existing[i]);
            return;
        }

        if (range.collapsed) {
            var empty = document.createElement(tagName);
            empty.appendChild(document.createTextNode('\u200B'));
            range.insertNode(empty);
            placeCaretInside(empty);
            return;
        }

        var wrapper = document.createElement(tagName);
        try {
            range.surroundContents(wrapper);
        } catch (e) {
            wrapper.appendChild(range.extractContents());
            range.insertNode(wrapper);
        }
        var wrapped = document.createRange();
        wrapped.selectNodeContents(wrapper);
        sel.removeAllRanges();
        sel.addRange(wrapped);
    }

    function clearTelegramFormats(editor) {
        editor = editor || getActiveRichEditor();
        if (!editor) return;
        var sel = global.getSelection();
        if (!sel || !sel.rangeCount) return;
        var range = sel.getRangeAt(0);
        var tags = ['code', 'pre', 'tg-spoiler', 'blockquote'];
        var toUnwrap = [];
        for (var t = 0; t < tags.length; t++) {
            var found = collectIntersectingFormats(range, tags[t], editor);
            for (var i = 0; i < found.length; i++) {
                if (toUnwrap.indexOf(found[i]) === -1) toUnwrap.push(found[i]);
            }
        }
        for (var j = toUnwrap.length - 1; j >= 0; j--) unwrapElement(toUnwrap[j]);
    }

    function replaceBlockTagsWithNewlines(html) {
        var text = String(html);
        text = text.replace(/<div><br\s*\/?><\/div>/gi, '\n');
        text = text.replace(/<p><br\s*\/?><\/p>/gi, '\n');
        text = text.replace(/<div\b[^>]*>/gi, '\n');
        text = text.replace(/<\/div>/gi, '');
        text = text.replace(/<p\b[^>]*>/gi, '\n');
        text = text.replace(/<\/p>/gi, '');
        text = text.replace(/<br\s*\/?>/gi, '\n');
        return text;
    }

    function extractTaggedBlocks(html) {
        var placeholders = [];
        var text = String(html).replace(/<(pre|blockquote)(\b[^>]*)>([\s\S]*?)<\/\1>/gi, function (match) {
            placeholders.push(match);
            return '\u0000PH' + (placeholders.length - 1) + '\u0000';
        });
        return { text: text, placeholders: placeholders };
    }

    function restorePlaceholders(text, placeholders) {
        return String(text).replace(/\u0000PH(\d+)\u0000/g, function (_, i) {
            return placeholders[Number(i)] || '';
        });
    }

    function htmlToTemplate(html) {
        if (!html) return '';
        var extracted = extractTaggedBlocks(html);
        var placeholders = extracted.placeholders.map(function (block) {
            return block.replace(/<(pre|blockquote)(\b[^>]*)>([\s\S]*?)<\/\1>/i, function (m, tag, attrs, inner) {
                return '<' + tag + attrs + '>' + replaceBlockTagsWithNewlines(inner) + '</' + tag + '>';
            });
        });
        var text = replaceBlockTagsWithNewlines(extracted.text);
        text = restorePlaceholders(text, placeholders);
        // Block tags are display:block in the editor, so a newline adjacent to
        // <pre>/<blockquote> may have been dropped during templateToHtml.
        // Restore it so save/reload keeps the surrounding line structure.
        text = text.replace(/([^\n])(<(?:pre|blockquote)\b)/gi, '$1\n$2');
        text = text.replace(/(<\/(?:pre|blockquote)>)([^\n])/gi, '$1\n$2');
        text = text.replace(/\u200B/g, '');
        text = text.replace(/\n\n+/g, '\n\n');
        return text.trim();
    }

    function templateToHtml(text) {
        if (!text) return '';
        var extracted = extractTaggedBlocks(text);
        var tokens = extracted.text.split(/(\u0000PH\d+\u0000)/);
        var result = '';
        var firstLineOpen = true;
        for (var i = 0; i < tokens.length; i++) {
            var token = tokens[i];
            var ph = token.match(PLACEHOLDER_RE);
            if (ph) {
                result += extracted.placeholders[Number(ph[1])] || '';
                firstLineOpen = false;
                continue;
            }
            if (!token) continue;
            var startsNewLine = false;
            if (i > 0 && PLACEHOLDER_RE.test(tokens[i - 1])) {
                if (token.charAt(0) === '\n') {
                    token = token.slice(1);
                    startsNewLine = true;
                }
            }
            if (i < tokens.length - 1 && PLACEHOLDER_RE.test(tokens[i + 1]) && token.charAt(token.length - 1) === '\n') {
                token = token.slice(0, -1);
            }
            if (!token) continue;
            var lines = token.split('\n');
            for (var idx = 0; idx < lines.length; idx++) {
                var line = lines[idx];
                if ((firstLineOpen && idx === 0) || (idx === 0 && !startsNewLine && result !== '')) {
                    result += line;
                } else {
                    result += '<div>' + (line || '<br>') + '</div>';
                }
            }
            firstLineOpen = false;
        }
        return result;
    }

    function patchRemoveFormat() {
        if (typeof global.cmd === 'function' && !global.cmd._richPatched) {
            var orig = global.cmd;
            var patched = function (command, value) {
                if (command === 'removeFormat') {
                    clearTelegramFormats();
                }
                return orig(command, value);
            };
            patched._richPatched = true;
            global.cmd = patched;
        }
    }

    if (global.document && global.document.addEventListener) {
        global.document.addEventListener('DOMContentLoaded', patchRemoveFormat);
        if (global.document.readyState !== 'loading') {
            patchRemoveFormat();
        }
    }

    global.wrapTelegramFormat = wrapTelegramFormat;
    global.clearTelegramFormats = clearTelegramFormats;
    global.htmlToTemplate = htmlToTemplate;
    global.templateToHtml = templateToHtml;
    global.getActiveRichEditor = getActiveRichEditor;

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            htmlToTemplate: htmlToTemplate,
            templateToHtml: templateToHtml
        };
    }
})(typeof window !== 'undefined' ? window : global);
