"""
plugins/_whatsapp_selectors.py — Authoritative, Centralized DOM Selectors & Stability Report for WhatsApp Web.

REAL DOM DISCOVERY REPORT (web.whatsapp.com):
----------------------------------------------
Mapped directly from live Chrome session on web.whatsapp.com:

1. Search Box:
   - Primary: 'input[role="textbox"][aria-label*="Search"]', '#side [role="textbox"]'
   - Attributes: role="textbox", aria-label="Search or start a new chat", placeholder="Search or start a new chat", data-tab="3"
   - Stability: VERY STABLE (semantic aria-label + role="textbox" + #side scoping).

2. Search Result Sections:
   - Container: '#side #pane-side div[role="grid"], #side div[data-testid="search-results"]'
   - Section Headings: 'h2' elements (e.g. "Chats", "Contacts", "Groups in common", "Messages").
   - Rows: 'div[role="row"], div[data-testid^="list-item-"], div[data-testid="cell-frame-container"]'
   - Row Title: '[data-testid="cell-frame-title"], span[title], span[dir="auto"]'
   - Stability: STABLE (H2 section dividers + role="row" / list-item-* testids).

3. Active Chat Header:
   - Header Container: '#main header, [data-testid="conversation-header"]'
   - Header Title (Recipient Name): '[data-testid="conversation-info-header-chat-title"], #main header span[dir="auto"][title], #main header span[title]'
   - Header Subtitle (Distinguishing Person vs Group):
     * Groups: Subtitle contains comma-separated member names, member counts, or "click here for group info"
     * 1:1 Contacts: Subtitle contains "online", "last seen...", phone number, or "click here for contact info"
     * Self Chat: Contains "Message yourself"
   - Stability: VERY STABLE (data-testid="conversation-header" and "conversation-info-header-chat-title").

4. Message List & Rows:
   - Message List: '#main div[data-testid="conversation-panel-messages"], #main div[role="application"]'
   - Message Rows: '#main div[data-testid^="conv-msg-"], #main div[data-testid="msg-container"], #main div[role="row"]'
   - Message Unique ID: 'data-id' attribute (e.g. data-id="3EB..." or data-testid="conv-msg-<id>")
   - Direction Indicators:
     * Incoming: '[data-icon="tail-in"]', '[data-testid="tail-in"]', '.message-in', '[data-testid="author"]'
     * Outgoing: '[data-icon="tail-out"]', '[data-testid="tail-out"]', '.message-out', '[data-icon*="check"]', '[data-icon="status-time"]'
   - Message Text: 'span.selectable-text, .copyable-text'
   - Message Sender Name: 'span[data-testid="author"], div[data-pre-plain-text]' (regex parsed: "\\[.*?\\] (.*?): ")
   - Message Timestamp: 'div[data-pre-plain-text]' (regex parsed: "\\[(.*?)\\]"), or time text span
   - Links in Message: 'a[href]'
   - Stability: HIGH (semantic data-icon tail-in/tail-out and status-time/check icons + copyable-text data-pre-plain-text).

5. Delivery State Icons (Outgoing status ticks):
   - Pending (Local clock): 'span[data-icon="status-time"], [data-testid="status-time"]'
   - Sent (Single checkmark): 'span[data-icon="msg-check"], span[data-icon="check"], [data-testid="msg-check"]'
   - Delivered (Double checkmark): 'span[data-icon="msg-dblcheck"], [data-testid="msg-dblcheck"]'
   - Read (Blue double checkmark): 'span[data-icon="msg-dblcheck-ack"], [data-testid="msg-dblcheck-ack"]'
   - Stability: VERY STABLE (unique data-icon attributes).

6. Message Composer & Send:
   - Composer: '[data-testid="conversation-compose-box-input"], footer div[contenteditable="true"][role="textbox"]'
   - Send Button: 'button[aria-label="Send"], button:has(span[data-icon="send"]), [data-testid="send"]'
   - Stability: VERY STABLE (data-testid="conversation-compose-box-input" and aria-label="Send").

7. Unread Badges:
   - '[aria-label*="unread"], [aria-label*="no leídos"], span[data-testid="icon-unread-count"]'
   - Stability: STABLE (aria-label text + icon-unread-count testid).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WhatsAppSelectors:
    """Isolated, centralized DOM selectors for web.whatsapp.com."""

    # 1. Authentication / QR Code
    qr_canvas: str = 'canvas[aria-label*="Scan"], canvas[aria-label*="código QR"], canvas[role="img"]'
    qr_container: str = 'div[data-ref], [data-testid="qrcode"], div[data-testid="intro-title"]'
    intro_screen: str = 'div[data-testid="intro-title"], span:has-text("To use WhatsApp on your computer")'

    # 2. Syncing / Startup Progress
    startup_progress: str = 'progress, [data-testid="startup-progress"], div[role="progressbar"]'
    syncing_spinner: str = 'span[data-testid="syncing-spinner"], div:has-text("Loading your chats"), div:has-text("Connecting")'

    # 3. Main Panels & Chat List
    chat_list_panel: str = '#side, [data-testid="chat-list"], div[id="pane-side"]'
    main_chat_panel: str = '#main, [data-testid="conversation-panel-wrapper"]'
    chat_list_scroll_container: str = '#pane-side, div[id="pane-side"]'

    # 4. Search Box
    search_input: str = (
        '#side [role="textbox"][aria-label*="Search"], '
        '#side input[role="textbox"], '
        '#side [placeholder*="Search"], '
        'div[contenteditable="true"][data-tab="3"], '
        '[data-testid="chat-list-search"], '
        'div[title="Search input textbox"], '
        '#side input[type="text"]'
    )
    search_results_container: str = '#pane-side div[role="grid"], div[data-testid="search-results"], #pane-side'
    search_result_item: str = (
        'div[role="row"], div[data-testid^="list-item-"], div[data-testid="cell-frame-container"], div[role="listitem"]'
    )
    search_cancel_btn: str = (
        'button[aria-label="Cancel search"], button[aria-label="Close search"], '
        '[data-testid="search-cancel"], [data-testid="search-back"], button[aria-label*="Back"]'
    )
    search_section_heading: str = '#side h2, #pane-side h2, [role="heading"][aria-level="2"]'

    # 5. Active Conversation Header
    chat_header: str = '#main header, header[data-testid="conversation-header"], header'
    chat_header_title: str = (
        '[data-testid="conversation-info-header-chat-title"], '
        '#main header span[dir="auto"][title], '
        '#main header span[title], '
        '#main header span[dir="auto"]'
    )
    chat_header_subtitle: str = (
        '[data-testid="conversation-info-header-chat-subtitle"], '
        '#main header span[title*="group info"], '
        '#main header span[title*="contact info"], '
        '#main header [data-testid="selectable-text"], '
        '#main header div[role="button"] span[dir="auto"]:last-child'
    )

    # 6. Message Composer & Send
    compose_box: str = (
        '[data-testid="conversation-compose-box-input"], '
        'footer div[contenteditable="true"][role="textbox"], '
        'footer div[contenteditable="true"], '
        'footer [role="textbox"]'
    )
    send_button: str = (
        'button[aria-label="Send"], button:has(span[data-icon="send"]), '
        'span[data-icon="send"], [data-testid="send"]'
    )

    # 7. Outgoing Message Delivery Ticks
    message_outgoing: str = (
        'div.message-out, '
        '[data-testid="msg-container"]:has(span[data-icon*="check"]), '
        '[data-testid="msg-container"]:has(span[data-icon="status-time"]), '
        '[data-testid="msg-container"]:has([data-icon="tail-out"]), '
        '[data-testid="conv-msg-"]:has([data-icon="tail-out"])'
    )
    tick_pending_clock: str = 'span[data-icon="status-time"], [data-testid="status-time"]'
    tick_sent_single: str = 'span[data-icon="msg-check"], span[data-icon="check"], [data-testid="msg-check"]'
    tick_delivered_double: str = 'span[data-icon="msg-dblcheck"], [data-testid="msg-dblcheck"]'
    tick_read_blue: str = 'span[data-icon="msg-dblcheck-ack"], [data-testid="msg-dblcheck-ack"]'

    # 8. Incoming / Outgoing Messages Inspection
    message_panel: str = '#main div[data-testid="conversation-panel-messages"], #main div[role="application"]'
    message_row: str = (
        '#main div[role="row"], #main div[data-testid^="conv-msg-"], '
        '#main div[data-testid="msg-container"], div.message-in, div.message-out'
    )
    message_container: str = '[data-testid="msg-container"], div.message-in, div.message-out'
    tail_incoming: str = '[data-icon="tail-in"], [data-testid="tail-in"], .message-in'
    tail_outgoing: str = '[data-icon="tail-out"], [data-testid="tail-out"], .message-out'
    message_text: str = 'span.selectable-text, div.copyable-text, span[dir="ltr"]'
    message_author: str = 'span[data-testid="author"], div[data-pre-plain-text]'
    message_time: str = 'div[data-pre-plain-text], span[dir="auto"]'
    message_links: str = 'a[href]'

    # 9. Unread Badges in Chat List
    unread_badge: str = (
        '[aria-label*="unread"], [aria-label*="no leídos"], '
        '[data-testid="icon-unread-count"], span[data-testid="icon-unread-count"]'
    )


SELECTORS = WhatsAppSelectors()
