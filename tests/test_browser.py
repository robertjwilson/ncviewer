"""The window in a browser: page 1 fills in the directories down from the
folders' layout, Continue reads the files, and the viewer maps a variable
once its file type is chosen. Skipped where Chromium is not installed."""

import pytest

from ncviewer import app as ncapp


@pytest.fixture
def page(browser):
    app = ncapp.App()
    url = app.start(0)
    tab = browser.new_page(viewport={"width": 1300, "height": 900})
    errors = []
    tab.on("pageerror", lambda error: errors.append(str(error)))
    tab.goto(url)
    yield tab, app
    assert errors == []
    tab.close()
    app.close()


def test_files_page_finds_the_depth(page, simulation):
    tab, app = page
    tab.fill("#f-simdir", simulation)
    tab.wait_for_function("() => document.querySelector('#f-ndown').value === '2'")
    tab.wait_for_selector("#ndown-found:not(.is-finding):not([hidden])")
    assert tab.text_content("#ndown-found") == "48 netCDF files found"
    assert "YYYY/MM/**.nc" in tab.text_content("#ndown-layout")
    assert "is-detected" in tab.get_attribute("#f-ndown", "class")
    assert tab.is_enabled("#continue")
    # typing a depth makes it the user's own
    tab.fill("#f-ndown", "1")
    tab.wait_for_selector("#m-files .is-nofiles")
    assert "is-detected" not in tab.get_attribute("#f-ndown", "class")
    assert not tab.is_enabled("#continue")


def test_viewer(page, simulation):
    tab, app = page
    tab.fill("#f-simdir", simulation)
    tab.wait_for_function("() => !document.querySelector('#continue').disabled")
    tab.click("#continue")
    tab.wait_for_selector("#view-viewer:not([hidden])", timeout=60000)
    tab.fill("#v-search", "chloro")
    assert tab.locator("#v-list li").count() == 1
    tab.fill("#v-search", "temp")
    tab.keyboard.press("Enter")
    # in two file types: nothing is plotted until one is chosen
    assert "Which do you want?" in tab.text_content("#v-streams")
    assert tab.locator("#plot .bk-Figure, #plot [class^=bk]").count() == 0
    tab.click("#v-streams label.ptr-mode >> nth=1")
    tab.wait_for_selector("#plot [class^=bk]", timeout=30000)
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
    assert "ptrc_T" in tab.text_content("#plot-notes")
    # the map fills its box, not a strip of axes
    assert tab.locator("#plot .bk-Figure, #plot > div").first.bounding_box()["height"] > 300
    tab.click("#v-next")
    tab.wait_for_function("() => document.querySelector('#v-when').textContent.startsWith('2000-02')")
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
    assert "2000/02/" in tab.text_content("#plot-notes")
    # a deeper level is drawn too
    tab.select_option("#v-level", "3")
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
    assert tab.is_hidden("#plot-error")
    # nothing but the map: no other plots to choose
    assert tab.locator("#v-kind, #point, #c-cell").count() == 0
    tab.click("#back")
    tab.wait_for_selector("#view-files:not([hidden])")
