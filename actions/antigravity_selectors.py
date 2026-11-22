"""
actions/antigravity_selectors.py — Centralized DOM and ARIA selectors for Antigravity IDE and Agent Panel.

All CSS and ARIA selectors for the IDE workbench, auxiliary sidebar,
and embedded agent panel are kept strictly in this single file so any upstream
UI change requires editing only this module.
"""

from __future__ import annotations

# Workbench & Auxiliary Sidebar Selectors
AUXILIARY_BAR = "#workbench\\.parts\\.auxiliarybar"
AGENT_PANEL_CONTAINER = "#workbench\\.parts\\.auxiliarybar, [aria-label*='Antigravity, Antigravity']"
TOGGLE_AGENT_PANEL = (
    "button[aria-label*='Antigravity Section'], "
    "button[aria-label*='Toggle Secondary Side Bar'], "
    "button[aria-label*='Hide Secondary Side Bar'], "
    "button[aria-label*='Show Secondary Side Bar'], "
    "[aria-label*='Secondary Side Bar']"
)

# Webview Frame Selectors
WEBVIEW_IFRAME = "iframe.webview, iframe[src*='extensionView=true'], iframe"

# Agent Panel Interior Selectors
MODEL_SELECTOR_BUTTON = (
    "[data-testid='model-selector'], "
    "[aria-label*='Select Model'], "
    "button[role='combobox'], "
    ".model-selector-btn, "
    ".model-dropdown"
)
MODEL_OPTIONS = (
    "[role='option'], "
    ".model-option, "
    "[data-testid='model-option'], "
    "li[role='menuitem']"
)
ACTIVE_MODEL_LABEL = (
    "[data-testid='selected-model'], "
    "[data-testid='model-selector'] span, "
    "button[role='combobox'] span, "
    ".current-model-name"
)

PROMPT_COMPOSER = (
    "textarea[placeholder*='Ask'], "
    "textarea[aria-label*='Prompt'], "
    "textarea[aria-label*='Ask'], "
    "div[contenteditable='true'], "
    "textarea"
)
SEND_BUTTON = (
    "button[aria-label*='Send'], "
    "button[data-testid*='send'], "
    "button.send-button"
)
STOP_BUTTON = (
    "button[aria-label*='Stop'], "
    "button[data-testid*='stop'], "
    "button.stop-button"
)
NEW_CONVERSATION_BUTTON = (
    "button[aria-label*='New Conversation'], "
    "button[aria-label*='New Chat'], "
    "button[data-testid*='new-conversation'], "
    "button[aria-label*='Reset']"
)

# Agent Activity Indicators
RUNNING_INDICATOR = (
    "[data-testid*='generating'], "
    "[data-testid*='streaming'], "
    ".agent-running, "
    ".spinner, "
    ".thinking-indicator"
)

# Review & Diff Changes Selectors
REVIEW_CONTAINER = (
    ".review-changes, "
    "[data-testid='review-container'], "
    ".file-changes-container, "
    ".diff-summary"
)
CHANGED_FILE_ITEM = (
    ".changed-file, "
    "[data-testid='changed-file'], "
    ".file-diff-item, "
    ".diff-file-entry"
)
ACCEPT_ALL_BUTTON = (
    "button[aria-label*='Accept all'], "
    "button:has-text('Accept all'), "
    "button.accept-all, "
    "[data-testid='accept-all-btn']"
)
REJECT_ALL_BUTTON = (
    "button[aria-label*='Reject all'], "
    "button:has-text('Reject all'), "
    "button.reject-all, "
    "[data-testid='reject-all-btn']"
)
PER_FILE_ACCEPT = "button[aria-label*='Accept'], button:has-text('Accept')"
PER_FILE_REJECT = "button[aria-label*='Reject'], button:has-text('Reject')"

# Terminal Command Approval Prompts (NEVER AUTO-APPROVE)
APPROVAL_CONTAINER = (
    ".terminal-approval, "
    "[data-testid='command-approval'], "
    ".approval-banner, "
    ".command-run-prompt"
)
APPROVAL_BUTTONS = (
    "button:has-text('Allow'), "
    "button:has-text('Run'), "
    "button:has-text('Approve'), "
    "button[aria-label*='Allow'], "
    "button[aria-label*='Run command']"
)
APPROVAL_COMMAND_TEXT = (
    ".command-preview, "
    "[data-testid='command-text'], "
    "code.command-snippet"
)

# Error and Quota Banners
ERROR_QUOTA_BANNER = (
    ".error-banner, "
    "[data-testid='error-banner'], "
    ".quota-exceeded, "
    ".alert-danger, "
    ".banner-error"
)
