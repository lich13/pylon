"""Exercise the locked browser runtime without account data or network access."""
from playwright.sync_api import sync_playwright
from pylon.shared.playwright_env import ensure


def main():
    ensure()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content('<main><button onclick="this.textContent=\'fixture done\'">fixture</button></main>')
            page.get_by_role('button', name='fixture', exact=True).click()
            assert page.get_by_role('button', name='fixture done', exact=True).is_visible()
            print('Locked Chromium launch and interaction passed.')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
