import os

from simulations import nemo_simulation

from ncviewer import files


def test_layout_year_month(simulation):
    assert files.folder_layout(simulation) == {"pattern": "YYYY/MM/**.nc", "ndown": 2}


def test_layout_month(tmp_path):
    root = nemo_simulation(str(tmp_path), years=(2000,), layout="MM")
    assert files.folder_layout(root) == {"pattern": "MM/*.nc", "ndown": 1}


def test_layout_flat(tmp_path):
    root = nemo_simulation(str(tmp_path), years=(2000,), layout="flat")
    assert files.folder_layout(root) == {"pattern": "*.nc", "ndown": 0}


def test_no_layout(tmp_path):
    assert files.folder_layout(str(tmp_path)) is None
    os.makedirs(tmp_path / "other")
    assert files.folder_layout(str(tmp_path)) is None
    assert files.folder_layout(str(tmp_path / "missing")) is None


def test_paths_and_filters(simulation):
    every = files.simulation_paths(simulation, 2)
    assert len(every) == 48
    assert len(files.simulation_paths(simulation, 2, require=["grid_T"])) == 24
    assert len(files.simulation_paths(simulation, 2, exclude=["grid_T"])) == 24
    assert files.simulation_paths(simulation, 1) == []


def test_restart_files_are_left_out(tmp_path):
    (tmp_path / "restart_0001.nc").write_bytes(b"")
    (tmp_path / "out_0001.nc").write_bytes(b"")
    assert [os.path.basename(path) for path in files.simulation_paths(str(tmp_path), 0)] == ["out_0001.nc"]


def test_streams_by_pattern():
    assert files.file_pattern("/a/amm7_1m_20000101_20000131_grid_T.nc") == "amm7_1m_**_**_grid_T.nc"
    assert files.file_pattern("x_1d_19990101.nc") == files.file_pattern("x_1d_20101231.nc")


def test_probe(simulation, tmp_path):
    found = files.probe(simulation, "", "", "")
    assert found["directory"] == "found"
    assert found["layout"]["ndown"] == 2
    assert found["files"] is None
    found = files.probe(simulation, "2", "", "ptrc")
    assert (found["files"], found["all_files"]) == (24, 48)
    assert files.probe(str(tmp_path / "nowhere"), "0")["directory"] == "missing"
    assert files.probe("", "0")["directory"] == "empty"
    assert files.probe(simulation, "-1")["ndown"] is None
