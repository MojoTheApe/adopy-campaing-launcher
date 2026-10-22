"""Select and read back the visible Facebook Page control; refuse ambiguous UI."""
from __future__ import annotations

import re


def ensure_page_identity(page, page_id: str, name: str = "", unique_name: bool = False) -> bool:
    if not page_id or not page_id.isdigit():
        return False
    try:
        control = page.get_by_role("combobox", name=re.compile(r"^Facebook Page$", re.I))
        # Native controls expose the actual ID and require no name inference.
        if control.count() == 1 and control.is_visible() and control.evaluate("el => el.tagName.toLowerCase()") == "select":
            control.select_option(value=page_id, timeout=5000)
            return control.input_value() == page_id
        # Meta's custom picker exposes names. Only a unique catalog name can
        # establish identity; duplicate names require an ID-bearing option.
        if not name or not unique_name:
            return False

        # Captured simple_create_2026_09_27: the collapsed Identity summary
        # has a level-4 heading, followed by its selected Page value. The
        # ad-set Page combobox elsewhere on the screen is not ad Identity.
        heading = page.get_by_role("heading", name="Facebook Page", exact=True, level=4)
        editor = page.locator("#identityEditorComponent")

        def selected() -> bool:
            if heading.count() == 1 and heading.is_visible():
                return heading.evaluate("el => (el.parentElement.nextElementSibling?.innerText || '').trim()") == name
            if editor.count() == 1 and editor.is_visible():
                # Only the selected-value view, after the chooser has closed.
                if page.get_by_role("dialog").count() or page.get_by_role("listbox").count():
                    return False
                return editor.get_by_role("button", name="Edit", exact=True).is_visible() and editor.get_by_text(name, exact=True).count() == 1
            return False

        if selected():
            return True
        if heading.count() == 1 and heading.is_visible():
            heading.locator("xpath=ancestor::*[@role='button'][1]").click(timeout=5000)
        if editor.count() != 1 or not editor.is_visible():
            return False
        editor.get_by_role("button", name="Edit", exact=True).click(timeout=5000)
        chooser = page.get_by_role("dialog")
        scope = chooser if chooser.count() == 1 and chooser.is_visible() else editor
        option = scope.get_by_role("option", name=name, exact=True)
        if option.count() != 1:
            option = scope.get_by_role("button", name=name, exact=True)
            if option.count() != 1:
                return False
        option.click(timeout=5000)
        # Bounded wait for React's selected-value render, not a click-only success.
        for _ in range(10):
            if selected():
                return True
            page.wait_for_timeout(100)
        return False
    except Exception:
        # An unfamiliar layout must never leave a draft marked publishable.
        return False
