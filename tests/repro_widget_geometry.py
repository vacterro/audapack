import asyncio
import sys

from playwright.async_api import async_playwright

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

async def measure(width, height, zoom, bound_project=None):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            viewport={'width': width, 'height': height},
            device_scale_factor=zoom
        )
        await page.set_content('''<!DOCTYPE html>
<html>
<head><title>ChatGPT</title></head>
<body style="margin:0; padding:0; width:100vw; height:100vh; overflow:hidden;">
  <div id="__next">
    <div style="display:flex; height:100vh;">
      <nav style="width:260px; height:100%; background:#171717;" id="sidebar"></nav>
      <main style="flex:1; display:flex; flex-direction:column; height:100%;">
        <div style="flex:1;"></div>
        <div style="padding:20px;">
          <textarea id="prompt-textarea" style="width:100%; height:50px;"></textarea>
        </div>
      </main>
    </div>
  </div>
</body>
</html>''')

        with open('resources/AUDAPACK_WIDGET.user.js', 'r', encoding='utf-8') as f:
            script_text = f.read()

        await page.evaluate('''() => {
            window.__ACB_ENABLE_TEST_HOOK__ = true;
            const store = {
                'ai_chatbuttons_bridge_token_v1': 'test-token'
            };
            window.GM_setValue = (k, v) => { store[k] = v; };
            window.GM_getValue = (k, defVal) => {
                return (k in store) ? store[k] : defVal;
            };
            window.GM_deleteValue = (k) => { delete store[k]; };
            window.GM_listValues = () => Object.keys(store);
            window.GM_addStyle = (css) => {
                const s = document.createElement('style');
                s.textContent = css;
                document.head.appendChild(s);
            };
            window.GM_xmlhttpRequest = (opts) => {
                if (opts.onload) {
                    opts.onload({
                        status: 200,
                        responseText: JSON.stringify({
                            ok: true,
                            projects: [
                                { project_id: 'smart_vac_cleaner', display_name: 'Smart VAC Cleaner', audit_name: 'Smart VAC Cleaner' },
                                { project_id: 'smart_vac_duplicate_remover', display_name: '_SMART_VAC_DUPLICATE_REMOVER', audit_name: '_SMART_VAC_DUPLICATE_REMOVER' },
                                { project_id: 'smart_vac_media_compressor', display_name: '_SMART_VAC_MEDIA_COMPRESSOR', audit_name: '_SMART_VAC_MEDIA_COMPRESSOR' },
                                { project_id: 'audapack', display_name: '_AUDAPACK', audit_name: '_AUDAPACK' },
                                { project_id: 'fastprompter', display_name: 'FastPrompter', audit_name: 'FastPrompter' },
                                { project_id: 'protrail', display_name: 'ProTrail', audit_name: 'ProTrail' }
                            ]
                        })
                    });
                }
            };
            window.GM_addValueChangeListener = () => {};
        }''')

        await page.evaluate(script_text)

        if bound_project:
            await page.evaluate(f'''() => {{
                const api = window.__ACB_TEST__;
                api.setManualArchiveBinding({{ project_id: '{bound_project}', display_name: '{bound_project}' }});
                api.renderManualArchiveControl();
            }}''')

        await page.evaluate('''() => {
            const api = window.__ACB_TEST__;
            api.state.superCompact = true;
            api.state.collapsed = false;
            api.applyDisplayState();
        }''')
        await page.wait_for_timeout(100)

        compact_measurements = await page.evaluate('''() => {
            const popup = document.querySelector('#acb-popup');
            const titlebar = document.querySelector('#acb-titlebar');
            const zipBtn = document.querySelector('#acb-manual-zip-btn');
            const superControls = document.querySelector('#acb-super-controls');
            const superBrand = document.querySelector('#acb-super-brand');
            const profileToggle = document.querySelector('#acb-super-profile-toggle');
            const autoLabel = document.querySelector('#acb-super-auto-label');
            const superProgress = document.querySelector('#acb-super-progress');
            const superState = document.querySelector('#acb-super-state');
            const newChat = document.querySelector('#acb-new-chat');
            const settingsBtn = document.querySelector('#acb-settings-btn');

            const rect = (el) => el ? el.getBoundingClientRect() : null;
            return {
                popup: rect(popup),
                titlebar: rect(titlebar),
                zipBtn: { rect: rect(zipBtn), text: zipBtn ? zipBtn.textContent : '' },
                superBrand: { rect: rect(superBrand), text: superBrand ? superBrand.textContent : '' },
                profileToggle: { rect: rect(profileToggle), text: profileToggle ? profileToggle.textContent : '' },
                superState: { rect: rect(superState), text: superState ? superState.textContent : '' },
                settingsBtn: { rect: rect(settingsBtn), text: settingsBtn ? settingsBtn.textContent : '' },
                newChat: rect(newChat),
                availableWidth: window.innerWidth,
                availableHeight: window.innerHeight
            };
        }''')

        await page.evaluate('''() => {
            const api = window.__ACB_TEST__;
            return api.openManualArchivePicker();
        }''')
        await page.wait_for_timeout(200)

        compact_picker_measurements = await page.evaluate('''() => {
            const popup = document.querySelector('#acb-popup');
            const menu = document.querySelector('#acb-manual-zip-menu');
            const rect = (el) => el ? el.getBoundingClientRect() : null;
            const pRect = rect(popup);
            const mRect = rect(menu);
            return {
                popup: pRect,
                menu: mRect,
                visibleHeight: mRect && pRect ? Math.max(0, Math.min(pRect.bottom, mRect.bottom) - Math.max(pRect.top, mRect.top)) : 0
            };
        }''')

        screenshot_compact_picker = f'tests/screenshot_compact_picker_{width}_{zoom}.png'
        await page.screenshot(path=screenshot_compact_picker)

        await page.evaluate('''() => {
            const api = window.__ACB_TEST__;
            api.closeManualArchivePicker();
            api.state.superCompact = false;
            api.state.collapsed = false;
            api.applyDisplayState();
        }''')
        await page.wait_for_timeout(100)

        await page.evaluate('''() => {
            const api = window.__ACB_TEST__;
            return api.openManualArchivePicker();
        }''')
        await page.wait_for_timeout(200)

        expanded_measurements = await page.evaluate('''() => {
            const popup = document.querySelector('#acb-popup');
            const menu = document.querySelector('#acb-manual-zip-menu');
            const list = document.querySelector('#acb-manual-zip-menu-list');
            const items = Array.from(list ? list.querySelectorAll('button') : []).map(b => ({
                text: b.textContent,
                clientWidth: b.clientWidth,
                scrollWidth: b.scrollWidth,
                clipped: b.scrollWidth > b.clientWidth,
                rect: b.getBoundingClientRect()
            }));
            const rect = (el) => el ? el.getBoundingClientRect() : null;
            return {
                popup: { rect: rect(popup), scrollHeight: popup ? popup.scrollHeight : 0, clientHeight: popup ? popup.clientHeight : 0 },
                menu: { rect: rect(menu), scrollHeight: menu ? menu.scrollHeight : 0, clientHeight: menu ? menu.clientHeight : 0 },
                items: items
            };
        }''')

        screenshot_expanded = f'tests/screenshot_expanded_{width}_{zoom}.png'
        await page.screenshot(path=screenshot_expanded)

        await browser.close()
        return {
            'width': width,
            'height': height,
            'zoom': zoom,
            'compact': compact_measurements,
            'compact_picker': compact_picker_measurements,
            'expanded': expanded_measurements,
            'screenshot_compact_picker': screenshot_compact_picker,
            'screenshot_expanded': screenshot_expanded
        }

async def main():
    matrix = [
        (800, 600, 1.0),
        (1280, 800, 1.0),
        (1280, 800, 1.25),
        (1280, 800, 1.5),
        (1920, 1080, 1.0),
    ]
    for w, h, z in matrix:
        res = await measure(w, h, z, bound_project='_SMART_VAC_DUPLICATE_REMOVER')
        print(f"=== Viewport {w}x{h}, Zoom {z} ===")
        print(f"  Compact popup width: {res['compact']['popup']['width']}px, titlebar: {res['compact']['titlebar']['width']}px")
        print(f"  ZIP btn: {res['compact']['zipBtn']['rect']['width']}px, text: '{res['compact']['zipBtn']['text']}'")
        print(f"  Brand: {res['compact']['superBrand']['rect']['width']}px, text: '{res['compact']['superBrand']['text']}'")
        print(f"  Profile: {res['compact']['profileToggle']['rect']['width']}px, text: '{res['compact']['profileToggle']['text']}'")
        print(f"  State: {res['compact']['superState']['rect']['width']}px, text: '{res['compact']['superState']['text']}'")
        print(f"  Compact Picker: menu={res['compact_picker']['menu']}, visibleHeight={res['compact_picker']['visibleHeight']}px")
        print(f"  Expanded popup rect: {res['expanded']['popup']['rect']}")
        print(f"  Picker menu rect: {res['expanded']['menu']['rect']}")
        for item in res['expanded']['items']:
            print(f"    Item: '{item['text']}' clipped={item['clipped']} (client={item['clientWidth']}, scroll={item['scrollWidth']})")

if __name__ == '__main__':
    asyncio.run(main())
