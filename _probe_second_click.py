# One-off diagnostic probe (read-only for the repo): reproduce the second
# manualArchiveZipAction click and dump composer state before it.
import asyncio
import sys

sys.path.insert(0, ".")

from playwright.async_api import async_playwright

from tests.test_browser_bridge_widget_integration import BridgeFixture, setup_widget_page


async def main() -> None:
    bridge = BridgeFixture()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await setup_widget_page(page, bridge, initial_path="/c/chat-a-1234")

            await page.click("#acb-manual-zip-btn")
            await page.wait_for_timeout(500)
            await page.click('#acb-manual-zip-menu-list button[data-project-id="audapack"]')
            await page.wait_for_timeout(1500)

            state = await page.evaluate("""() => {
                const api = window.__ACB_TEST__;
                const input = (typeof api.getChatGPTInput === 'function') ? api.getChatGPTInput() : 'NO_HOOK';
                const root = (typeof api.chatGPTComposerRoot === 'function') ? api.chatGPTComposerRoot() : 'NO_HOOK';
                return {
                    hookNames: Object.keys(api).filter(k => /composer|input|snapshot|manual/i.test(k)).slice(0, 40),
                    inputType: input === 'NO_HOOK' ? 'NO_HOOK' : String(input && input.tagName),
                    rootType: root === 'NO_HOOK' ? 'NO_HOOK' : String(root && root.tagName),
                    textarea: !!document.querySelector('#prompt-textarea'),
                    formPresent: !!document.querySelector('form[data-type="unified-composer"]'),
                    tiles: document.querySelectorAll('#attachment-tiles-container [role="group"]').length,
                };
            }""")
            print("STATE:", state)

            second = await page.evaluate("() => window.__ACB_TEST__.manualArchiveZipAction()")
            print("SECOND_OK:", second.get("ok"), "CODE:", second.get("code"), "ERR:", second.get("errorCode"))
            await browser.close()
    finally:
        bridge.teardown()


asyncio.run(main())
