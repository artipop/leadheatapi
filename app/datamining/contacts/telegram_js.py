"""JavaScript snippets for Telegram Web DOM automation via Playwright `page.evaluate`."""

# Detect that an active chat pane is visible and has core chat UI mounted.
# language=javascript
JS_IS_ACTIVE_CHAT_OPEN = """() => {
    const isVisible = (chat) => {
        const rect = chat.getBoundingClientRect();
        const style = window.getComputedStyle(chat);
        return (
            rect.width > 260 &&
            rect.height > 260 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden'
        );
    };
    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
        || chats.find((chat) => isVisible(chat));
    if (!active) return false;
    const hasHeader = !!active.querySelector('.chat-info-container, .sidebar-header.topbar');
    const hasChatBody = !!active.querySelector('.bubbles, .bubbles-inner, .chat-input, .chat-background');
    return hasHeader || hasChatBody;
}"""

# Read runtime state used for channel stabilization/reload logic.
# language=javascript
JS_CHANNEL_RUNTIME_STATE = """() => {
    const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const isVisible = (chat) => {
        const rect = chat.getBoundingClientRect();
        const style = window.getComputedStyle(chat);
        return (
            rect.width > 260 &&
            rect.height > 260 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden'
        );
    };
    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
        || chats.find((chat) => isVisible(chat));
    const headerText = normalize(
        active?.querySelector('.chat-info-container, .sidebar-header.topbar')?.textContent || ''
    );
    const bodyText = normalize(document.body?.innerText || '');
    const waiting =
        bodyText.includes('waiting for network') ||
        bodyText.includes('updating...') ||
        bodyText.includes('обновление') ||
        bodyText.includes('ожидание сети');
    const bubblesCount = (active || document).querySelectorAll('.bubble, .channel-post').length;
    const repliesCount = (active || document).querySelectorAll('replies-element, .replies-footer, .replies-footer-text').length;
    return {
        has_active_chat: !!active,
        header_text: headerText,
        waiting_network: waiting,
        bubbles_count: bubblesCount,
        replies_count: repliesCount,
    };
}"""

# Pick best matching chatlist href for target channel hash/username.
# language=javascript
JS_FIND_BEST_CHAT_HREF = """([targetKey, targetUserNorm]) => {
    const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const compact = (s) => normalize(s).replace(/[^a-z0-9а-яё]+/g, '');
    const links = Array.from(document.querySelectorAll('a[href]'));
    let best = null;
    let bestScore = -1;
    for (const link of links) {
        const href = normalize(link.getAttribute('href') || '');
        const text = normalize(link.textContent || '');
        const textCompact = compact(text);
        if (!href && !text) continue;
        let score = 0;
        if (targetKey && href.includes(targetKey)) score += 100;
        if (targetUserNorm && textCompact.includes(targetUserNorm)) score += 40;
        if (link.className && String(link.className).includes('chatlist-chat')) score += 20;
        const rect = link.getBoundingClientRect();
        const style = window.getComputedStyle(link);
        const visible =
            rect.width > 0 &&
            rect.height > 0 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none';
        if (!visible) score -= 100;
        if (score > bestScore) {
            best = link;
            bestScore = score;
        }
    }
    if (best && bestScore > 20) {
        return best.getAttribute('href') || '';
    }
    return '';
}"""

# Click best matching chatlist row directly in DOM scoring by target hash/username.
# language=javascript
JS_CLICK_BEST_CHAT_LINK = """([targetKey, targetUserNorm]) => {
    const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const compact = (s) => normalize(s).replace(/[^a-z0-9а-яё]+/g, '');
    const links = Array.from(document.querySelectorAll('a[href]'));
    let best = null;
    let bestScore = -1;

    for (const link of links) {
        const href = normalize(link.getAttribute('href') || '');
        const text = normalize(link.textContent || '');
        const textCompact = compact(text);
        if (!href && !text) continue;

        let score = 0;
        if (targetKey && href.includes(targetKey)) score += 100;
        if (targetUserNorm && textCompact.includes(targetUserNorm)) score += 40;
        if (link.className && String(link.className).includes('chatlist-chat')) score += 20;

        const rect = link.getBoundingClientRect();
        const style = window.getComputedStyle(link);
        const visible =
            rect.width > 0 &&
            rect.height > 0 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none';
        if (!visible) score -= 100;

        if (score > bestScore) {
            best = link;
            bestScore = score;
        }
    }

    if (best && bestScore > 20) {
        best.click();
        return true;
    }
    return false;
}"""

# Use sidebar search input and click best chat match by username when hash lookup fails.
# language=javascript
JS_SEARCH_AND_CLICK_CHAT_BY_USERNAME = """([rawUser, targetUserNorm]) => {
    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const compact = (s) => normalize(s).replace(/[^a-z0-9а-яё]+/g, '');

    const searchCandidates = Array.from(
        document.querySelectorAll(
            '.sidebar-left input[type="text"], .sidebar-left input,' +
            '.chatlist-container input[type="text"], .chatlist-container input,' +
            'input[placeholder*="Search" i], input[placeholder*="Поиск" i],' +
            '[contenteditable="true"][data-placeholder*="Search" i],' +
            '[contenteditable="true"][data-placeholder*="Поиск" i]'
        )
    ).filter((el) => isVisible(el));

    const input = searchCandidates.find((el) => {
        const ph = normalize(el.getAttribute('placeholder') || el.getAttribute('aria-label') || el.getAttribute('data-placeholder') || '');
        return ph.includes('search') || ph.includes('поиск') || ph.includes('find');
    }) || searchCandidates[0];

    if (!input) return false;

    input.focus();
    if ('value' in input) {
        input.value = '';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.value = rawUser;
    } else {
        input.textContent = '';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.textContent = rawUser;
    }
    input.dispatchEvent(new Event('input', { bubbles: true }));

    const links = Array.from(document.querySelectorAll('a[href], .chatlist-chat'));
    let best = null;
    let bestScore = -1;
    for (const link of links) {
        const text = normalize(link.textContent || '');
        const textCompact = compact(text);
        if (!text) continue;
        let score = 0;
        if (targetUserNorm && textCompact.includes(targetUserNorm)) score += 80;
        const rect = link.getBoundingClientRect();
        const style = window.getComputedStyle(link);
        const visible =
            rect.width > 0 &&
            rect.height > 0 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none';
        if (!visible) score -= 100;
        if (score > bestScore) {
            best = link;
            bestScore = score;
        }
    }
    if (best && bestScore > 20) {
        best.click();
        return true;
    }
    return false;
}"""

# Prefer exact click on a global-search result matching @username.
# Returns clicked row href (often "#-<peer_id>") or empty string.
# language=javascript
JS_CLICK_SEARCH_RESULT_BY_USERNAME = """([rawUser, targetUserNorm]) => {
    const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const compact = (s) => normalize(s).replace(/[^a-z0-9а-яё]+/g, '');
    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
    };

    const atUser = `@${normalize(rawUser).replace(/^@+/, '')}`;
    const searchInput = Array.from(
        document.querySelectorAll(
            '.sidebar-left input[type="text"], .sidebar-left input,' +
            '.chatlist-container input[type="text"], .chatlist-container input,' +
            'input[placeholder*="Search" i], input[placeholder*="Поиск" i]'
        )
    ).find((el) => isVisible(el));
    if (!searchInput) return '';

    searchInput.focus();
    searchInput.value = '';
    searchInput.dispatchEvent(new Event('input', { bubbles: true }));
    searchInput.value = normalize(rawUser).replace(/^@+/, '');
    searchInput.dispatchEvent(new Event('input', { bubbles: true }));

    const rows = Array.from(
        document.querySelectorAll(
            '.chatlist .chatlist-chat, .chatlist a[href], .search-super-container a[href], .search-super-container .chatlist-chat'
        )
    ).filter((row) => isVisible(row));

    let best = null;
    let bestScore = -1;
    for (const row of rows) {
        const text = normalize(row.textContent || '');
        const href = normalize(row.getAttribute('href') || '');
        if (!text && !href) continue;
        let score = 0;
        if (href.includes(atUser)) score += 120;
        if (text.includes(atUser)) score += 120;
        if (targetUserNorm && compact(text).includes(targetUserNorm)) score += 50;
        if (String(row.className || '').includes('chatlist-chat')) score += 20;
        if (score > bestScore) {
            best = row;
            bestScore = score;
        }
    }

    if (!best || bestScore < 60) return '';
    const clickedHref = (best.getAttribute('href') || '').trim();
    best.click();
    return clickedHref;
}"""

# Detect whether current view already looks like discussion/group chat.
# language=javascript
JS_IS_GROUP_CHAT_OPEN = """() => {
    const isVisible = (chat) => {
        const rect = chat.getBoundingClientRect();
        const style = window.getComputedStyle(chat);
        return (
            rect.width > 260 &&
            rect.height > 260 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden'
        );
    };
    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
        || chats.find((chat) => isVisible(chat));
    if (!active) return false;
    const header = active.querySelector('.chat-info-container, .sidebar-header.topbar');
    const headerText = (header?.textContent || '').toLowerCase();
    if (/subscribers?|подписчик/.test(headerText)) return false;
    if (/comments?|discussion|обсужд|коммент/.test(headerText)) return true;
    if (/members?|участник/.test(headerText)) return true;
    const sidebar = document.querySelector('.sidebar.sidebar-right');
    const sidebarText = (sidebar?.textContent || '').toLowerCase();
    if (/channel info|информация о канале/.test(sidebarText)) return false;
    if (/group info|информация о группе/.test(sidebarText)) return true;
    const hasMemberRows = !!sidebar?.querySelector('[data-peer-id]');
    return /members|участник/.test(sidebarText) && hasMemberRows;
}"""

# Open active chat header "three dots" menu (rightmost toggle in header).
# language=javascript
JS_CLICK_ACTIVE_CHAT_MENU = """() => {
    const isVisible = (chat) => {
        const rect = chat.getBoundingClientRect();
        const style = window.getComputedStyle(chat);
        return (
            rect.width > 260 &&
            rect.height > 260 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden'
        );
    };
    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
        || chats.find((chat) => isVisible(chat));
    if (!active) return false;

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));

    const buttons = Array.from(active.querySelectorAll('.chat-info-container button.btn-menu-toggle')).filter((button) => {
        const rect = button.getBoundingClientRect();
        const style = window.getComputedStyle(button);
        return (
            rect.width > 0 &&
            rect.height > 0 &&
            rect.top >= 0 &&
            rect.top < 120 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none'
        );
    });
    if (!buttons.length) return false;

    buttons.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
    buttons[0].click();
    return true;
}"""

# Backward-compatible alias requested by user ("like CLICK_ACTIVE_CHAT_MENU_JS").
CLICK_ACTIVE_CHAT_MENU_JS = JS_CLICK_ACTIVE_CHAT_MENU

# Click "View discussion" item in opened top-right menu.
# language=javascript
JS_CLICK_DISCUSSION_MENU_ITEM = """() => {
    const pattern = /(view\\s*discussion|discussion|обсужд)/i;
    const items = Array.from(document.querySelectorAll('.btn-menu-item')).filter((item) => {
        const rect = item.getBoundingClientRect();
        const style = window.getComputedStyle(item);
        return (
            rect.width > 0 &&
            rect.height > 0 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none' &&
            item.offsetParent !== null
        );
    });
    const target = items.find((item) => pattern.test((item.textContent || '').replace(/\\s+/g, ' ').trim()));
    if (!target) return false;
    target.click();
    return true;
}"""

# Click visible "Leave a comment"/"Comments" footer inside active channel post.
# language=javascript
JS_CLICK_LEAVE_COMMENT_OR_COMMENTS = """() => {
    const pattern = /(leave\\s+a\\s+comment|view\\s+comments|comments?|коммент|обсужд)/i;
    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return (
            rect.width > 0 &&
            rect.height > 0 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none'
        );
    };
    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
        || chats.find((chat) => isVisible(chat));
    if (!active) return false;

    const replies = Array.from(
        active.querySelectorAll('replies-element.replies-footer, .replies-footer, .replies-footer-text')
    ).filter((el) => {
        const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
        return text && pattern.test(text) && isVisible(el);
    });
    if (replies.length) {
        replies.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
        replies[0].click();
        return true;
    }

    const candidates = Array.from(active.querySelectorAll('button, a, div, span')).filter((el) => {
        const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
        return text && pattern.test(text) && isVisible(el);
    });
    if (!candidates.length) return false;
    candidates.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
    candidates[0].click();
    return true;
}"""

# Generic fallback: heuristically find any clickable discussion/comment action.
# language=javascript
JS_CLICK_DISCUSSION_ANYWHERE = """() => {
    const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const patternStrong = /(view\\s*discussion|open\\s*comments|leave\\s+a\\s+comment|обсужд|коммент)/i;
    const patternWeak = /(comments?)/i;

    const isVisible = (el) => {
        if (!(el instanceof Element)) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        const style = window.getComputedStyle(el);
        return !(style.visibility === 'hidden' || style.display === 'none');
    };

    const isClickable = (el) => {
        if (!(el instanceof Element)) return false;
        if (el.matches('button, a, [role="button"], .btn-menu-item, .row-clickable, .rp')) return true;
        const onclickAttr = el.getAttribute('onclick');
        if (onclickAttr) return true;
        const style = window.getComputedStyle(el);
        return style.cursor === 'pointer';
    };

    const clickableAncestor = (el) => {
        let node = el;
        for (let i = 0; i < 6 && node; i += 1) {
            if (isClickable(node) && isVisible(node)) return node;
            node = node.parentElement;
        }
        return null;
    };

    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
        || chats.find((chat) => {
            const rect = chat.getBoundingClientRect();
            const style = window.getComputedStyle(chat);
            return (
                rect.width > 260 &&
                rect.height > 260 &&
                style.display !== 'none' &&
                style.visibility !== 'hidden'
            );
        });
    const roots = [
        ...(active ? [active] : []),
        ...Array.from(document.querySelectorAll('.btn-menu-item, .popup-container, .btn-menu')),
    ];
    if (!roots.length) roots.push(document.body);

    const seen = new Set();
    const candidates = [];
    for (const root of roots) {
        const nodes = root.querySelectorAll('*');
        for (const node of nodes) {
            if (!(node instanceof Element) || !isVisible(node)) continue;
            const text = normalize(node.textContent);
            if (!text || text.length > 140) continue;
            if (!patternStrong.test(text) && !patternWeak.test(text)) continue;
            const clickable = clickableAncestor(node);
            if (!clickable) continue;
            if (seen.has(clickable)) continue;
            seen.add(clickable);

            let score = 0;
            if (/view\\s*discussion|обсужд/.test(text)) score += 50;
            if (/open\\s*comments|leave\\s+a\\s+comment|коммент/.test(text)) score += 30;
            if (/comments?/.test(text)) score += 10;
            if (clickable.matches('.btn-menu-item')) score += 20;
            if (clickable.matches('button, a')) score += 10;
            score -= Math.max(0, text.length - 40) / 10;
            candidates.push({clickable, score});
        }
    }

    candidates.sort((a, b) => b.score - a.score);
    const target = candidates[0]?.clickable;
    if (!target) return false;
    target.click();
    return true;
}"""

# Dump diagnostics around active chat/menu/comment-like text for error rows.
# language=javascript
JS_DISCUSSION_DEBUG_SNAPSHOT = """() => {
    const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
    const isVisible = (chat) => {
        const rect = chat.getBoundingClientRect();
        const style = window.getComputedStyle(chat);
        return (
            rect.width > 260 &&
            rect.height > 260 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden'
        );
    };
    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
        || chats.find((chat) => isVisible(chat));
    const activeHeaderText = normalize(
        active?.querySelector('.chat-info-container, .sidebar-header.topbar')?.textContent || ''
    );

    const menuItems = Array.from(document.querySelectorAll('.btn-menu-item'))
        .map((el) => normalize(el.textContent))
        .filter(Boolean)
        .slice(0, 20);

    const anchors = Array.from(document.querySelectorAll('a[href]'))
        .map((el) => ({
            href: normalize(el.getAttribute('href') || ''),
            text: normalize(el.textContent || '').slice(0, 120),
            cls: normalize(String(el.className || '')).slice(0, 120),
        }))
        .slice(0, 60);

    const chatlistLike = anchors.filter((item) => /chatlist-chat|row-clickable|chatlist/.test(item.cls)).slice(0, 30);

    const commentLike = Array.from(
        (active || document).querySelectorAll('button, a, div, span')
    )
        .map((el) => normalize(el.textContent))
        .filter((t) => /(discussion|comment|обсужд|коммент)/i.test(t))
        .slice(0, 30);

    const activeUrl = location.href;
    return {
        active_url: activeUrl,
        active_header_text: activeHeaderText.slice(0, 220),
        menu_items: menuItems,
        discussion_like_texts: commentLike,
        anchors_count: anchors.length,
        anchors_sample: anchors.slice(0, 20),
        chatlist_links_sample: chatlistLike,
    };
}"""

# Detect whether right sidebar currently displays group info (not channel info).
# language=javascript
JS_IS_GROUP_INFO_OPEN = """() => {
    const sidebar = document.querySelector('.sidebar.sidebar-right');
    if (!sidebar) return false;
    const text = (sidebar.textContent || '').toLowerCase();
    if (/channel info|информация о канале/.test(text)) return false;
    if (/group info|информация о группе/.test(text)) return true;
    return /members|участники/.test(text) && !!sidebar.querySelector('[data-peer-id]');
}"""

# Open right sidebar by clicking active chat header.
# language=javascript
JS_OPEN_GROUP_INFO_BY_HEADER_CLICK = """() => {
    const isVisible = (chat) => {
        const rect = chat.getBoundingClientRect();
        const style = window.getComputedStyle(chat);
        return (
            rect.width > 260 &&
            rect.height > 260 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden'
        );
    };
    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
        || chats.find((chat) => isVisible(chat));
    const header = active?.querySelector('.chat-info-container, .sidebar-header.topbar');
    if (!header) return false;
    header.click();
    return true;
}"""

# Click SUBSCRIBE/JOIN if visible (optional flow).
# language=javascript
JS_CLICK_SUBSCRIBE_OR_JOIN = """() => {
    const pattern = /(subscribe|join|подпис|вступить)/i;
    const buttons = Array.from(document.querySelectorAll('button, a'));
    const target = buttons.find((el) => {
        const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!text || !pattern.test(text)) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
    });
    if (!target) return false;
    target.click();
    return true;
}"""

# Switch to the Members tab inside right sidebar.
# language=javascript
JS_SELECT_MEMBERS_TAB = """() => {
    const sidebar = document.querySelector('.sidebar.sidebar-right') || document;
    const normalized = (text) => (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return (
            rect.width > 0 &&
            rect.height > 0 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden'
        );
    };
    const isClickable = (el) => {
        if (!el) return false;
        if (el.matches('button, a, [role="tab"], [role="button"]')) return true;
        const cls = String(el.className || '');
        if (/row-clickable|rp|menu-horizontal-div-item|tabs|tab/i.test(cls)) return true;
        const style = window.getComputedStyle(el);
        return style.cursor === 'pointer';
    };
    const clickableAncestor = (el) => {
        let node = el;
        for (let i = 0; i < 6 && node; i += 1) {
            if (isClickable(node) && isVisible(node)) return node;
            node = node.parentElement;
        }
        return null;
    };

    const pool = Array.from(sidebar.querySelectorAll('*'));
    const candidates = [];
    for (const node of pool) {
        if (!isVisible(node)) continue;
        const text = normalized(node.textContent);
        if (!text || text.length > 48) continue;
        if (!(text === 'members' || text === 'участники' || text.includes('members') || text.includes('участник'))) {
            continue;
        }
        const target = clickableAncestor(node);
        if (!target) continue;
        candidates.push(target);
    }
    if (!candidates.length) return false;
    candidates[0].click();
    return true;
}"""

# Extract visible members rows from sidebar list.
# language=javascript
JS_EXTRACT_VISIBLE_MEMBERS = """() => {
    const rows = Array.from(
        document.querySelectorAll(
            '.sidebar.sidebar-right .search-super-container-members a.chatlist-chat-abitbigger,' +
            '.sidebar.sidebar-right .search-super-container-members .chatlist-chat-abitbigger,' +
            '.sidebar.sidebar-right .search-super-container-members .chatlist-chat,' +
            '.sidebar.sidebar-right a.chatlist-chat-abitbigger[data-peer-id],' +
            '.sidebar.sidebar-right .chatlist-chat-abitbigger[data-peer-id],' +
            '.sidebar.sidebar-right .chatlist-chat[data-peer-id]'
        )
    );
    return rows.map((row) => {
        const peerId = (
            row.getAttribute('data-peer-id') ||
            row.querySelector('[data-peer-id]')?.getAttribute('data-peer-id') ||
            ''
        ).trim();
        const nameNode = row.querySelector('.fullName, .peer-title, .user-title, .title, .full-name');
        const statusNode = row.querySelector('.subtitle, .status, .user-status, .user-last-seen');
        const name = (nameNode?.textContent || '').replace(/\\s+/g, ' ').trim();
        const status = (statusNode?.textContent || '').replace(/\\s+/g, ' ').trim();
        const rawText = (row.textContent || '').replace(/\\s+/g, ' ').trim();
        return { peer_id: peerId, name, status, raw_text: rawText };
    });
}"""

# Check if members list rows are already rendered.
# language=javascript
JS_HAS_MEMBERS_LIST_ROWS = """() => {
    const sidebar = document.querySelector('.sidebar.sidebar-right');
    if (!sidebar) return false;
    const memberContainer = sidebar.querySelector('.search-super-container-members.tabs-tab.active, .search-super-container-members');
    if (!memberContainer) return false;
    return !!memberContainer.querySelector(
        '[data-peer-id], a.chatlist-chat-abitbigger, .chatlist-chat-abitbigger, .chatlist-chat'
    );
}"""

# Scroll members list container down to load more rows.
# language=javascript
JS_SCROLL_MEMBERS_LIST = """() => {
    const row = document.querySelector(
        '.sidebar.sidebar-right .search-super-container-members [data-peer-id],' +
        '.sidebar.sidebar-right .search-super-container-members a.chatlist-chat-abitbigger,' +
        '.sidebar.sidebar-right .search-super-container-members .chatlist-chat-abitbigger,' +
        '.sidebar.sidebar-right .search-super-container-members .chatlist-chat,' +
        '.sidebar.sidebar-right a.chatlist-chat-abitbigger[data-peer-id],' +
        '.sidebar.sidebar-right .chatlist-chat-abitbigger[data-peer-id],' +
        '.sidebar.sidebar-right .chatlist-chat[data-peer-id]'
    );
    if (!row) return false;
    let container = row.parentElement;
    while (container && container !== document.body) {
        if (container.scrollHeight > container.clientHeight + 4) {
            const previousTop = container.scrollTop;
            container.scrollTop = previousTop + Math.max(220, Math.floor(container.clientHeight * 0.85));
            return container.scrollTop > previousTop;
        }
        container = container.parentElement;
    }
    return false;
}"""

# Login-state check for Telegram Web (auth screen markers absent + chat UI present).
# language=javascript
JS_IS_LOGGED_IN_TELEGRAM_WEB = """() => {
    const loginMarkers = [
        ...document.querySelectorAll('input, button, div, span')
    ].some((el) => {
        const text = (el.textContent || '').toLowerCase();
        return (
            text.includes('log in') ||
            text.includes('sign in') ||
            text.includes('войти') ||
            text.includes('phone number') ||
            text.includes('номер телефона')
        );
    });
    const chatListExists = !!document.querySelector('.chatlist-container, .tabs-container');
    return chatListExists && !loginMarkers;
}"""
