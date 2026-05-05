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

    const isClickable = (el) => {
        if (!(el instanceof Element)) return false;
        if (el.matches('button, a, [role="button"], [role="link"], .row-clickable, .rp, .btn, .ripple-handler')) {
            return true;
        }
        const cls = String(el.className || '');
        if (/row-clickable|rp|btn|ripple|replies-footer|clickable/i.test(cls)) return true;
        const style = window.getComputedStyle(el);
        return style.cursor === 'pointer' || !!el.getAttribute('onclick');
    };
    const clickableAncestor = (el) => {
        let node = el;
        for (let i = 0; i < 8 && node; i += 1) {
            if (isClickable(node) && isVisible(node)) return node;
            node = node.parentElement;
        }
        return null;
    };

    const replies = Array.from(
        active.querySelectorAll('replies-element.replies-footer, .replies-footer, .replies-footer-text')
    ).filter((el) => {
        const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
        return text && pattern.test(text) && isVisible(el);
    });
    if (replies.length) {
        replies.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
        const target = clickableAncestor(replies[0]) || replies[0];
        target.click();
        return true;
    }

    const candidates = Array.from(active.querySelectorAll('button, a, div, span')).filter((el) => {
        const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
        return text && pattern.test(text) && isVisible(el);
    });
    if (!candidates.length) return false;
    candidates.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
    const target = clickableAncestor(candidates[0]) || candidates[0];
    target.click();
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
    const extractUsername = (value) => {
        const text = String(value || '');
        const linkMatch = text.match(/(?:https?:\\/\\/)?(?:t\\.me|telegram\\.me)\\/(@?[A-Za-z0-9_]{5,32})(?:[/?#]|$)/i);
        if (linkMatch) return linkMatch[1].replace(/^@/, '');
        const hashMatch = text.match(/#@([A-Za-z0-9_]{5,32})(?:[/?#]|$)/);
        if (hashMatch) return hashMatch[1];
        const mentionMatch = text.match(/@([A-Za-z0-9_]{5,32})/);
        if (mentionMatch) return mentionMatch[1];
        return '';
    };
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
        const href = (
            row.getAttribute('href') ||
            row.querySelector('a[href]')?.getAttribute('href') ||
            ''
        ).trim();
        const username = extractUsername(`${href} ${rawText}`);
        return {
            peer_id: peerId,
            name,
            status,
            raw_text: rawText,
            href,
            username,
            public_url: username ? `https://t.me/${username}` : '',
        };
    });
}"""

# Try to resolve public usernames from Telegram Web runtime stores by internal peer id.
# This is much faster and less fragile than opening each profile card, but Telegram
# Web does not expose these globals consistently across builds, so it is best-effort.
# language=javascript
JS_EXTRACT_USERNAMES_BY_PEER_IDS = """async (peerIds) => {
    const ids = Array.from(new Set((peerIds || []).map((id) => String(id || '').trim()).filter(Boolean)));
    const output = {};
    const normalizeUsername = (value) => {
        const username = String(value || '').replace(/^@/, '').trim();
        if (!/^[A-Za-z0-9_]{5,32}$/.test(username)) return '';
        return username;
    };
    const assign = (peerId, value) => {
        const username = normalizeUsername(value);
        if (username && !output[peerId]) {
            output[peerId] = { username, public_url: `https://t.me/${username}` };
        }
    };
    const readUser = (peerId, user) => {
        if (!user || typeof user !== 'object') return;
        assign(peerId, user.username);
        assign(peerId, user.usernames?.[0]?.username);
        assign(peerId, user.usernames?.[0]);
        assign(peerId, user.user?.username);
        assign(peerId, user._?.username);
    };
    const managers = [
        window.appUsersManager,
        window.managers?.appUsersManager,
        window.managers?.users,
        window.telegram?.appUsersManager,
        window.Telegram?.appUsersManager,
    ].filter(Boolean);

    for (const peerId of ids) {
        for (const manager of managers) {
            try {
                if (typeof manager.getUser === 'function') readUser(peerId, await manager.getUser(peerId));
                if (typeof manager.getUserById === 'function') readUser(peerId, await manager.getUserById(peerId));
                if (typeof manager.get === 'function') readUser(peerId, await manager.get(peerId));
                readUser(peerId, manager.users?.[peerId]);
                readUser(peerId, manager.users?.get?.(peerId));
            } catch (_) {}
            if (output[peerId]) break;
        }
    }
    return output;
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

# Click a visible member row in the right sidebar by Telegram internal peer id.
# Used only for optional profile enrichment; rows may be virtualized, so this
# works for currently rendered members and callers should invoke it before
# scrolling away from the visible batch.
# language=javascript
JS_CLICK_VISIBLE_MEMBER_BY_PEER_ID = """(peerId) => {
    const selector = [
        '.sidebar.sidebar-right .search-super-container-members a.chatlist-chat-abitbigger',
        '.sidebar.sidebar-right .search-super-container-members .chatlist-chat-abitbigger',
        '.sidebar.sidebar-right .search-super-container-members .chatlist-chat',
        '.sidebar.sidebar-right .search-super-container-members [data-peer-id]',
        '.sidebar.sidebar-right a.chatlist-chat-abitbigger[data-peer-id]',
        '.sidebar.sidebar-right .chatlist-chat-abitbigger[data-peer-id]',
        '.sidebar.sidebar-right .chatlist-chat[data-peer-id]'
    ].join(',');
    const rows = Array.from(document.querySelectorAll(selector));
    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const row = rows.find((el) => {
        const value = el.getAttribute('data-peer-id') || el.querySelector('[data-peer-id]')?.getAttribute('data-peer-id') || '';
        return value.trim() === String(peerId || '').trim() && isVisible(el);
    });
    if (!row) return false;
    row.click();
    return true;
}"""

# Extract a public username from an opened Telegram user profile/sidebar when visible.
# Telegram Web changes DOM class names often, so this intentionally combines href
# extraction with text heuristics for @username and t.me links.
# language=javascript
JS_EXTRACT_OPEN_USER_PROFILE_USERNAME = """() => {
    const containers = Array.from(document.querySelectorAll(
        '.sidebar.sidebar-right, .popup, .profile, .user-profile, .chat-info'
    ));
    const visibleContainers = containers.filter((el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 120 && rect.height > 120 && style.display !== 'none' && style.visibility !== 'hidden';
    });
    const root = visibleContainers[visibleContainers.length - 1] || document.body;
    const ignore = new Set([
        'joinchat', 'share', 'addstickers', 'proxy', 'iv', 's', 'c'
    ]);
    const normalizeUsername = (value) => {
        const match = String(value || '').match(/@?([A-Za-z0-9_]{5,32})/);
        if (!match) return '';
        const username = match[1];
        if (ignore.has(username.toLowerCase())) return '';
        return username;
    };

    const anchors = Array.from(root.querySelectorAll('a[href]'));
    for (const anchor of anchors) {
        const href = anchor.getAttribute('href') || '';
        const text = anchor.textContent || '';
        const linkMatch = href.match(/(?:https?:\\/\\/)?(?:t\\.me|telegram\\.me)\\/(@?[A-Za-z0-9_]{5,32})(?:[/?#]|$)/i);
        if (linkMatch) {
            const username = normalizeUsername(linkMatch[1]);
            if (username) return { username, public_url: `https://t.me/${username}` };
        }
        const textMatch = text.match(/@([A-Za-z0-9_]{5,32})/);
        if (textMatch) {
            const username = normalizeUsername(textMatch[1]);
            if (username) return { username, public_url: `https://t.me/${username}` };
        }
    }

    const text = (root.textContent || '').replace(/\\s+/g, ' ');
    const textMatch = text.match(/@([A-Za-z0-9_]{5,32})/);
    if (textMatch) {
        const username = normalizeUsername(textMatch[1]);
        if (username) return { username, public_url: `https://t.me/${username}` };
    }
    return { username: '', public_url: '' };
}"""

# Return from an opened user profile back to the group info/members sidebar.
# language=javascript
JS_CLOSE_OPEN_USER_PROFILE = """() => {
    const visible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const candidates = Array.from(document.querySelectorAll(
        '.sidebar.sidebar-right button, .sidebar.sidebar-right .btn-icon, .sidebar.sidebar-right [role="button"], .popup button, .popup [role="button"]'
    ));
    const backOrClose = candidates.find((el) => {
        if (!visible(el)) return false;
        const label = [
            el.getAttribute('aria-label') || '',
            el.getAttribute('title') || '',
            el.className || '',
            el.textContent || ''
        ].join(' ').toLowerCase();
        return /back|назад|close|закрыть|btn-menu-toggle|tgico-left|tgico-close/.test(label);
    });
    if (backOrClose) {
        backOrClose.click();
        return true;
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
