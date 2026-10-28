# ncviewer

A window in your browser for looking at what a model simulation's output holds. Type in
the directory of the output and ncviewer finds every netCDF file, groups them into file
types, and reads the variables and times they hold. Then search for a variable and map it.

Its look and its first page come from the [OceanVal](https://github.com/PlymouthMarineLaboratory/OceanVal)
window. How it draws a map comes from [ncplot](https://github.com/pmlmodelling/ncplot).

## Install

With conda (recommended):

```sh
conda env create -f ncviewer.yml
conda activate ncviewer
pip install .
```

Or with pip alone, without coastlines: `pip install .`

## Use

```sh
ncviewer
```

This opens the window in your browser. You can also start it with a directory:
`ncviewer /path/to/output`. Over SSH, or with `--no-browser`, ncviewer prints the link to
open, and the `ssh -L` command to forward its port. Closing the browser tab stops ncviewer,
and so does Ctrl+C.

### Page 1: where the files are

- **Directory**: where the simulation's output is. As you type, the box suggests
  directories.
- **Directories down**: how many directories below that the files are. ncviewer fills it
  in (in amber) when it recognises the layout of the folders:

  | Layout          | Directories down |
  |-----------------|------------------|
  | `YYYY/MM/*.nc`  | 2                |
  | `MM/*.nc`       | 1                |
  | `*.nc`          | 0                |

  You can choose 0 to 5 from the box's list, or type any number.
- **File filters** (optional): words a file name must not contain, or must contain.
  Restart files are always left out.
- **Missing values** (optional): numbers that mean there is no data but are not marked as
  missing in the files, such as 0 or -999. Add one number, or fill in To as well for a
  range (both ends are included). Cells with these values are set to missing for every
  variable, before the map is drawn, so they are left blank and do not stretch the colour
  scale. Go Back to change them.

ncviewer counts the files as you type. **Continue** reads the times of every file (only
the time variable is read) and the variables of the first file of each file type. Files
are grouped into types by their names with the dates taken out, so `..._20000101_grid_T.nc`
and `..._20000201_grid_T.nc` are one type. What it reads is kept in `~/.ncviewercache`
(or the file `$NCVIEWERCACHE` names), so a directory opened again reads only new or
changed files.

### Page 2: the viewer

- **Left**: search the variables by name, description or units. Only variables that can be
  mapped (those with two horizontal dimensions) are listed. If a variable is in more than
  one file type, choose which one to use. Clicking a variable maps it alone.
  To map the **sum** of several variables, tick the boxes beside them (or press Shift+Enter
  on the highlighted one); **Clear** unticks them all. The variables must be in the same
  file type and have the same dimensions, so a variable that cannot be added to those
  ticked is dimmed. The sum is made in xarray before the map is drawn: your missing
  values are applied to each variable first, and a cell that is missing in any of them is
  missing in the sum. If the units differ, the sum is still drawn, so check they make sense added. Below
  that is what the variable is (its
  dimensions, files and times), followed by the controls:
  - **Time**: a slider, the arrow buttons (or the arrow keys), and **Play**, which animates
    the simulation from the time shown, and starts again at the beginning after the last.
    The frames are read ahead while it plays, so the map is not covered or blanked between
    them. It uses one colour scale for the whole run (worked out from time steps spread
    through it, so colours mean the same in every frame), and keeps your zoom. Choose
    **Slow**, **Normal** or **Fast** (2, 5 or 10 frames a second); it can only go as fast as
    the files can be read, and says "Loading" if it has to wait. Moving the slider pauses
    it. Pause leaves the colour scale as it was until you choose **Automatic scale**.
  - **Time series**: turn it on, then click the map to see the values of that cell through
    every time of the file type, at the depth shown (the sum, if several variables are
    ticked, with your missing values applied). The cell is the nearest one with a value in
    the map shown, within 2 cells of the click, so a click on the coast is taken to the sea.
    The series opens in a pop-out over the map. Drag it by its title to move it, close it with
    × (or Esc), and click elsewhere to replace it. Only that cell is read from each file
    (with h5py, or netCDF4 for netCDF-3 files, not xarray). With more than 6 files, the
    pop-out counts them as they are read. With 40 files or more they are read in a pool of
    processes: **Cores** says how many, half the machine's cores to start with. Compressed
    files are slower, as each time step's whole chunk is decompressed to get one cell, and
    more cores help most there.
  - **Depth**: the level to show.
  - **Colours**: the colour map, fixed limits ("Keep this scale for every time"), a
    logarithmic scale, and coastlines (needs cartopy and geoviews).
- **Middle**: the map. You can zoom, pan, hover to read values, and save a PNG.

Only the map being shown is read: one time step at one level of one file (a time series reads
one cell of each file). So output of many GB stays quick to look at.

### How a map is drawn (from ncplot)

| Grid | Drawn as |
|------|----------|
| Regular longitude/latitude | An image |
| Uneven or curvilinear | A quadmesh |
| Cells whose longitude/latitude cannot place them (land cells written as 0, for example) | An image on the grid's own indices |

When the values span 0, the colour scale is symmetric about 0 and drawn in `RdBu_r`.
Otherwise it is `viridis`. Very large grids are thinned out for drawing, and the page says
so when that happens.

## Tests

```sh
python -m pytest
```

The browser tests need playwright and Chromium. Without them they are skipped.
