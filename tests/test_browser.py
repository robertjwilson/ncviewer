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


def test_missing_values_on_the_files_page(page, simulation):
    tab, app = page
    tab.fill("#f-simdir", simulation)
    tab.wait_for_function("() => !document.querySelector('#continue').disabled")
    # the boxes are there from the start, empty, and an untouched row stops nothing
    assert tab.locator("#missing-rows .missing__row").count() == 1
    assert tab.is_visible("#missing-from-0") and tab.is_visible("#missing-to-0")
    assert tab.input_value("#missing-from-0") == ""
    assert tab.is_enabled("#continue")
    tab.fill("#missing-from-0", "abc")
    # a box that is not a number is marked, and Continue waits
    assert "is-invalid" in tab.get_attribute("#missing-from-0", "class")
    assert tab.is_disabled("#continue")
    assert "not a number" in tab.text_content("#m-files")
    tab.fill("#missing-from-0", "-999")
    tab.click("#missing-add")
    tab.fill("#missing-from-1", "10")
    tab.fill("#missing-to-1", "12.5")
    assert tab.is_enabled("#continue")
    # a row is removed by its button
    tab.click("#missing-add")
    tab.click("[aria-label='Remove missing value 3']")
    assert tab.locator("#missing-rows .missing__row").count() == 2
    # removing the last one leaves an empty row to fill in
    tab.click("[aria-label='Remove missing value 2']")
    tab.click("[aria-label='Remove missing value 1']")
    assert tab.locator("#missing-rows .missing__row").count() == 1
    assert tab.input_value("#missing-from-0") == ""
    tab.fill("#missing-from-0", "-999")
    tab.click("#missing-add")
    tab.fill("#missing-from-1", "10")
    tab.fill("#missing-to-1", "12.5")
    tab.click("#continue")
    tab.wait_for_function("() => document.querySelector('#view-viewer:not([hidden])')", timeout=60000)
    assert app.missing == [(-999.0, -999.0), (10.0, 12.5)]
    # going back shows them again
    tab.click("#back")
    tab.wait_for_selector("#view-files:not([hidden])")
    assert tab.input_value("#missing-from-1") == "10"


def test_viewer(page, simulation):
    tab, app = page
    tab.fill("#f-simdir", simulation)
    tab.wait_for_function("() => !document.querySelector('#continue').disabled")
    # the coldest cells of the first map, which the colour scale then leaves out
    tab.fill("#missing-from-0", "0")
    tab.fill("#missing-to-0", "11.3")
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
    assert "set to missing" in tab.text_content("#plot-notes")
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


def test_sum_of_variables(page, simulation):
    tab, app = page
    tab.fill("#f-simdir", simulation)
    tab.wait_for_function("() => !document.querySelector('#continue').disabled")
    tab.click("#continue")
    tab.wait_for_selector("#view-viewer:not([hidden])", timeout=60000)
    row = lambda name: f"#v-list li[data-name='{name}']"  # noqa: E731
    # tick chl: ptrc_T is its only file type, so it is chosen and drawn
    tab.check(f"{row('chl')} input")
    tab.wait_for_selector("#plot [class^=bk]", timeout=30000)
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
    # sossheig cannot be added to chl: not the same dimensions
    assert "is-incompatible" in tab.get_attribute(row("sossheig"), "class")
    assert tab.is_disabled(f"{row('sossheig')} input")
    # votemper can (it is in two file types, but only ptrc_T has chl)
    tab.check(f"{row('votemper')} input")
    tab.wait_for_function("() => document.querySelector('#v-info').textContent.includes('Sum of')")
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
    assert "chl (mg m-3) + votemper (degC)" in tab.text_content("#v-info")
    assert "units differ" in tab.text_content("#plot-notes")
    assert tab.is_hidden("#plot-error")
    # the file type is not asked: only one holds both
    assert tab.is_hidden("#v-streams")
    # Clear unticks everything
    tab.click("#v-clear")
    assert tab.locator("#v-list li input:checked").count() == 0
    assert tab.is_hidden("#v-chosen")
    # clicking a row's text chooses that one alone, and ticks only it
    tab.click(f"{row('sossheig')} .var-list__name")
    tab.wait_for_selector("#plot [class^=bk]", timeout=30000)
    assert tab.locator("#v-list li input:checked").count() == 1
    tab.click("#back")
    tab.wait_for_selector("#view-files:not([hidden])")
