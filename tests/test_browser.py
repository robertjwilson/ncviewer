"""The window in a browser: page 1 fills in the directories down from the
folders' layout, Continue reads the files, and the viewer maps a variable
once its file type is chosen. Skipped where Chromium is not installed."""

import os
import time

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
    assert tab.locator("#plot-wrap [class^=bk]").count() == 0
    tab.click("#v-streams label.ptr-mode >> nth=1")
    tab.wait_for_selector("#plot-wrap [class^=bk]", timeout=30000)
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
    # the calendar is not shown
    assert "Calendar" not in tab.text_content("#v-info")
    assert "Times" in tab.text_content("#v-info")
    # the first and last dates are one row, without the time of day
    info = tab.text_content("#v-info")
    assert "Dates2000-01-16 \u2013 2001-12-16" in info
    assert "From" not in info and "To2001" not in info
    # nothing is written under the map
    assert tab.locator("#plot-notes, .stage__notes").count() == 0
    # the map fills its box, not a strip of axes
    assert tab.locator("#plot-wrap [class^=bk]").first.bounding_box()["height"] > 300
    tab.click("#v-next")
    tab.wait_for_function("() => document.querySelector('#v-when').textContent.startsWith('2000-02')")
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
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
    tab.wait_for_selector("#plot-wrap [class^=bk]", timeout=30000)
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
    # sossheig cannot be added to chl: not the same dimensions
    assert "is-incompatible" in tab.get_attribute(row("sossheig"), "class")
    assert tab.is_disabled(f"{row('sossheig')} input")
    # votemper can (it is in two file types, but only ptrc_T has chl)
    tab.check(f"{row('votemper')} input")
    tab.wait_for_function("() => document.querySelector('#v-info').textContent.includes('Sum of')")
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
    assert "chl (mg m-3) + votemper (degC)" in tab.text_content("#v-info")
    assert tab.is_hidden("#plot-error")
    # the file type is not asked: only one holds both
    assert tab.is_hidden("#v-streams")
    # Clear unticks everything
    tab.click("#v-clear")
    assert tab.locator("#v-list li input:checked").count() == 0
    assert tab.is_hidden("#v-chosen")
    # clicking a row's text chooses that one alone, and ticks only it
    tab.click(f"{row('sossheig')} .var-list__name")
    tab.wait_for_selector("#plot-wrap [class^=bk]", timeout=30000)
    assert tab.locator("#v-list li input:checked").count() == 1
    tab.click("#back")
    tab.wait_for_selector("#view-files:not([hidden])")


def _open_chl(tab, simulation):
    tab.fill("#f-simdir", simulation)
    tab.wait_for_function("() => !document.querySelector('#continue').disabled")
    tab.click("#continue")
    tab.wait_for_selector("#view-viewer:not([hidden])", timeout=60000)
    tab.fill("#v-search", "chl")
    tab.keyboard.press("Enter")
    tab.wait_for_selector("#plot-wrap [class^=bk]", timeout=30000)
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")


def _watch_play(tab):
    """Records the dates shown, and any time the "reading the data" cover is up, from now on."""
    tab.evaluate("""() => {
        window.__seen = []; window.__covered = 0;
        const busy = document.getElementById('plot-busy');
        new MutationObserver(() => { if (!busy.hidden) window.__covered += 1; }).observe(busy, {attributes: true});
        const when = document.getElementById('v-when');
        new MutationObserver(() => window.__seen.push(when.textContent.slice(0, 10)))
            .observe(when, {childList: true, characterData: true, subtree: true});
    }""")


def test_play_is_an_animation(page, simulation):
    tab, app = page
    _open_chl(tab, simulation)
    plots = []
    tab.on("request", lambda request: plots.append(request.url) if "api/plot" in request.url else None)
    _watch_play(tab)
    tab.click("#v-play")
    # it moves through the times, and the map is never covered or empty
    tab.wait_for_function("() => window.__seen.length >= 4", timeout=30000)
    assert tab.evaluate("() => window.__covered") == 0
    assert tab.locator("#plot-wrap [class^=bk]:visible").count() > 0
    assert tab.get_attribute("#v-play", "aria-pressed") == "true"
    # each date is the next one in the run
    seen = tab.evaluate("() => window.__seen")
    assert seen[:2] == ["2000-02-15", "2000-03-16"]
    # one colour scale for every frame that was asked for
    scales = {tuple(url.split(name + "=")[1].split("&")[0] for name in ("vmin", "vmax")) for url in plots[1:]}
    assert len(scales) == 1 and "" not in next(iter(scales))
    # pausing stops it
    tab.click("#v-play")
    assert tab.get_attribute("#v-play", "aria-pressed") == "false"
    settled = tab.evaluate("() => window.__seen.length")
    tab.wait_for_timeout(800)
    assert tab.evaluate("() => window.__seen.length") <= settled + 1
    assert tab.is_hidden("#plot-error")


def test_play_speed(page, simulation):
    tab, app = page
    _open_chl(tab, simulation)
    assert tab.input_value("#v-speed") == "normal"
    rates = {}
    for speed in ("slow", "fast"):
        tab.select_option("#v-speed", speed)
        _watch_play(tab)
        tab.click("#v-play")
        tab.wait_for_timeout(3000)
        tab.click("#v-play")
        rates[speed] = tab.evaluate("() => window.__seen.length")
    assert rates["fast"] > rates["slow"] + 3


def _figure_ranges(tab, to=None):
    """The ranges of the figure on show (and sets them first, if to is given)."""
    return tab.evaluate("""(to) => {
        const views = Object.values(Bokeh.index).filter((v) => v.model && v.model.type === "Figure");
        const m = views[views.length - 1].model;
        if (to) { [m.x_range.start, m.x_range.end, m.y_range.start, m.y_range.end] = to; }
        return [m.x_range.start, m.x_range.end, m.y_range.start, m.y_range.end];
    }""", to)


def test_play_keeps_the_zoom_and_the_scale(page, simulation):
    tab, app = page
    _open_chl(tab, simulation)
    assert _figure_ranges(tab) == [-0.5, 9.5, -0.5, 11.5]
    # zoom in, then play: every frame is zoomed the same
    _figure_ranges(tab, [2, 6, 1, 5])
    _watch_play(tab)
    tab.click("#v-play")
    tab.wait_for_function("() => window.__seen.length >= 3", timeout=30000)
    tab.click("#v-play")
    assert _figure_ranges(tab) == [2, 6, 1, 5]
    # only the figure on show is kept, not one for every frame
    assert tab.evaluate("() => Object.values(Bokeh.index).filter((v) => v.model && v.model.type === 'Figure').length") == 1
    # the scale of the animation is kept after it, so the map does not change under the user,
    # until Automatic scale is chosen
    assert tab.input_value("#v-vmin") == ""
    tab.click("#v-auto")
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
    assert tab.is_hidden("#plot-error")
    # choosing a different variable starts from a fresh view
    tab.fill("#v-search", "sossheig")
    tab.keyboard.press("Enter")
    tab.wait_for_selector("#plot-busy[hidden]", state="attached")
    assert _figure_ranges(tab) == [-0.5, 9.5, -0.5, 11.5]


def _click_map(tab, x_share=0.5, y_share=0.5):
    """Clicks the map on show, at a share of its plotting area's width and height."""
    box = tab.evaluate("""() => {
        // the map's figure, not the time series'
        const views = Object.values(Bokeh.index).filter((v) => v.model && v.model.type === "Figure"
            && !document.getElementById("series").contains(v.el));
        const view = views[views.length - 1];
        const frame = view.frame.bbox, canvas = view.el.getBoundingClientRect();
        return [canvas.left + frame.left, canvas.top + frame.top, frame.width, frame.height];
    }""")
    # two clicks in quick succession are a double tap to Bokeh, not two taps
    tab.wait_for_timeout(500)
    tab.mouse.click(box[0] + box[2] * x_share, box[1] + box[3] * y_share)


def test_time_series_of_a_cell(page, simulation):
    tab, app = page
    _open_chl(tab, simulation)
    # off to start with: a click on the map does nothing
    assert tab.get_attribute("#v-series", "aria-pressed") == "false"
    assert tab.is_hidden("#v-series-hint")
    _click_map(tab)
    tab.wait_for_timeout(300)
    assert tab.is_hidden("#series")
    # 24 files: too few for the cores to matter
    assert tab.is_hidden("#v-cores-label")
    tab.click("#v-series")
    assert tab.get_attribute("#v-series", "aria-pressed") == "true"
    assert tab.is_visible("#v-series-hint")
    _click_map(tab, 0.6, 0.5)
    tab.wait_for_selector("#series:not([hidden])")
    tab.wait_for_selector("#series-plot [class^=bk]", timeout=30000)
    assert tab.is_hidden("#series-error")
    assert tab.is_hidden("#series-reading")
    assert tab.text_content("#series-title") == "Time series of chl"
    # the map is still there, and still answers clicks: a second one replaces the series
    first = app.series_job["cell"]
    # (above the pop-out, which is over the bottom of the map)
    _click_map(tab, 0.9, 0.1)
    for _ in range(100):
        if app.series_job and app.series_job["cell"] != first:
            break
        tab.wait_for_timeout(100)
    assert app.series_job["cell"] != first
    tab.wait_for_function("() => document.querySelector('#series-reading').hidden", timeout=30000)
    tab.wait_for_selector("#series-plot [class^=bk]", timeout=30000)
    # one chart, the old one taken apart
    assert tab.evaluate("() => Object.values(Bokeh.index).filter((v) => v.model && v.model.type === 'Figure').length") == 2
    tab.screenshot(path=os.environ.get("NCVIEWER_SCREENSHOT", "/dev/null"), type="png")
    # the x closes it, and stops it
    tab.click("#series-close")
    assert tab.is_hidden("#series")
    assert app.series_job is None
    # Escape does too
    _click_map(tab, 0.6, 0.5)
    tab.wait_for_selector("#series:not([hidden])")
    tab.keyboard.press("Escape")
    assert tab.is_hidden("#series")
    # turning it off closes it and makes clicks do nothing again
    _click_map(tab, 0.6, 0.5)
    tab.wait_for_selector("#series:not([hidden])")
    tab.click("#v-series")
    assert tab.is_hidden("#series")
    _click_map(tab, 0.6, 0.5)
    tab.wait_for_timeout(300)
    assert tab.is_hidden("#series")


def test_time_series_cores(page, simulation, monkeypatch):
    from ncviewer import series

    monkeypatch.setattr(series, "PARALLEL_FROM", 10)
    monkeypatch.setattr(series, "max_cores", lambda: 4)
    monkeypatch.setattr(series, "default_cores", lambda: 2)
    tab, app = page
    tab.reload()
    _open_chl(tab, simulation)
    tab.click("#v-series")
    # enough files for the cores to matter: half the machine's to start with
    assert tab.is_visible("#v-cores-label")
    assert tab.input_value("#v-cores") == "2"
    assert tab.text_content("#v-cores-of") == "of 4"
    tab.fill("#v-cores", "9")
    assert "is-invalid" in tab.get_attribute("#v-cores", "class")
    _click_map(tab)
    tab.wait_for_selector("#series-error:not([hidden])")
    assert "from 1 to 4" in tab.text_content("#series-error")
    tab.fill("#v-cores", "1")
    _click_map(tab)
    tab.wait_for_selector("#series-plot [class^=bk]", timeout=30000)
    assert tab.is_hidden("#series-error")


def test_time_series_counts_the_files(page, simulation, monkeypatch):
    from ncviewer import series

    # slow enough to be seen
    reading = series.point_values
    monkeypatch.setattr(series, "point_values", lambda *args: (time.sleep(0.15), reading(*args))[1])
    tab, app = page
    _open_chl(tab, simulation)
    tab.click("#v-series")
    _click_map(tab, 0.6, 0.6)
    # more than 6 files: counted, with a meter
    tab.wait_for_function("() => /Reading the files: [1-9]\\d*\\/24 files read/.test(document.querySelector('#series-reading-text').textContent)")
    assert tab.is_visible("#series-reading .meter")
    tab.screenshot(path=os.environ.get("NCVIEWER_SCREENSHOT_COUNT", "/dev/null"), type="png")
    tab.wait_for_selector("#series-plot [class^=bk]", timeout=30000)
    assert tab.is_hidden("#series-reading")
