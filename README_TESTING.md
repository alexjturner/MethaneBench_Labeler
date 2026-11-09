# Testing changes without touching the live site

The live labeler ([bench4.atmos.uw.edu](https://bench4.atmos.uw.edu)) runs from
`/home/disk/funnel/bench4` via `start_app.csh` on port **63951**, with its
database at `/home/disk/funnel/bench4/labels.db`. To try out a branch (e.g. GUI
changes), run a second copy from your own checkout on a different port, with
its own database and a small local subset of the imagery.

Nothing below reads from or writes to the live `labels.db`, and the live
server keeps running untouched.

## 1. Get a test imagery subset (one-time)

The full imagery set (~15 GB) lives in `/home/disk/funnel/bench4/imagery`.
Copy a ~2 GB subset of 20 files into `./imagery` (`*.nc` files there are
git-ignored). Each file holds all acquisitions for one site and one sensor.

The subset is drawn from sites in `calibration_scenes.txt` to cover all three
sensors and a range of regions and scene types:

| Site | Sentinel-2 | Landsat 8/9 | Landsat 4/5 |
|---|:-:|:-:|:-:|
| Korpeje compressor, Turkmenistan (CH4Net T17) | ✓ | ✓ | ✓ |
| Hassi R'Mel gas field, Algeria | ✓ | ✓ | ✓ |
| Permian Basin Midland compressor, TX | ✓ | ✓ | ✓ |
| Basra oil fields, Iraq | ✓ | | ✓ |
| Shanxi coal, Datong basin, China | ✓ | ✓ | |
| Kuzbass coal basin, Russia | ✓ | ✓ | |
| Yamburg gas field, W. Siberia (Arctic) | ✓ | | |
| Casablanca-Mediouna landfill, Morocco | | ✓ | |
| Niger Delta, Nigeria | ✓ | | |
| Lake Maracaibo, Venezuela | ✓ | | |
| CH4Net T23, Turkmenistan (negative example) | ✓ | | |

```tcsh
cd /path/to/MethaneBench_Labeler
set SRC = /home/disk/funnel/bench4/imagery
foreach f ( \
    sentinel2/sentinel2_channel_38.494_54.198.nc \
    landsat_89/landsat8_9_channel_38.494_54.198.nc \
    landsat_45/landsat4_5_channel_38.494_54.198.nc \
    sentinel2/sentinel2_channel_32.947_3.171.nc \
    landsat_89/landsat8_9_channel_32.947_3.171.nc \
    landsat_45/landsat4_5_channel_32.947_3.171.nc \
    sentinel2/sentinel2_channel_31.731_-102.042.nc \
    landsat_89/landsat8_9_channel_31.731_-102.042.nc \
    landsat_45/landsat4_5_channel_31.731_-102.042.nc \
    sentinel2/sentinel2_channel_30.509_47.816.nc \
    landsat_45/landsat4_5_channel_30.509_47.816.nc \
    sentinel2/sentinel2_channel_40.094_113.291.nc \
    landsat_89/landsat8_9_channel_40.094_113.291.nc \
    sentinel2/sentinel2_channel_54.011_86.096.nc \
    landsat_89/landsat8_9_channel_54.011_86.096.nc \
    sentinel2/sentinel2_channel_68.0_75.825.nc \
    landsat_89/landsat8_9_channel_33.483_-7.538.nc \
    sentinel2/sentinel2_channel_4.9_6.5.nc \
    sentinel2/sentinel2_channel_10.2_-71.6.nc \
    sentinel2/sentinel2_channel_37.717_53.927.nc )
  cp -p $SRC/$f imagery/$f
end
```

(Alternatively, set `IMAGERY_PATH=/home/disk/funnel/bench4/imagery` to test
against the full set. That's safe because the app only *reads* imagery, but
startup is slower.)

## 2. Run the test server

```tcsh
git checkout <your-branch>
pixi run preview
```

which is equivalent to:

```tcsh
setenv IMAGERY_PATH ./imagery
setenv DB_PATH ./test_labels.db
setenv MPLCONFIGDIR /tmp
pixi run uvicorn app:app --host 127.0.0.1 --port 8001
```

Wait for `Ready.` in the output, then open <http://localhost:8001>.

- If you're on the server (e.g. a remote desktop session), open it in a browser there.
- If you're on your laptop, forward the port first:
  `ssh -L 8001:localhost:8001 <user>@<server>`, then browse to
  <http://localhost:8001> locally. If the server runs on a compute node behind
  a login host, jump through the login host with `-J`. For example, with the
  test server on `h1` behind `hermes`:

  ```
  ssh -J hikarim@hermes.atmos.uw.edu -L 8001:localhost:8001 hikarim@h1
  ```

Changes to `static/` (HTML/JS/CSS) only need a browser refresh. Changes to
`app.py` need a server restart (Ctrl-C, then `pixi run preview` again), or add
`--reload` to the uvicorn command.

## Things to keep in mind

- **Never use port 63951.** That's the live server.
- **Keep `--host 127.0.0.1`.** `0.0.0.0` would expose the test server publicly.
- **Test labels go to `test_labels.db`** (git-ignored). Delete it to start fresh.
- **Calibration warnings are expected.** With only the subset loaded, startup
  prints `Calibration scene NOT FOUND` for some entries, and others are
  "snapped" to the nearest loaded site with the same acquisition index. This
  only affects the test copy.
- `check_running.csh` (the live watchdog) only looks at the live user's own
  processes, so your test server won't interfere with it. 
# GEE imagery and reference scenes on the server

The `gee-imagery` branch adds imagery pulled from Google Earth Engine
(`gee_fetch.py`), reference scenes with known answers (MARS-S2L and the
Stanford controlled releases) and calibration lists built from them. Below,
`$DATA` is a data folder on the server outside the live `bench4` folder.

## 1. Code and Earth Engine login (one-time)

```tcsh
cd /path/to/MethaneBench_Labeler
git fetch && git checkout gee-imagery
pixi install
pixi run earthengine authenticate --auth_mode=notebook   # headless: paste the code from the browser
```

The pulls use the GEE project `aerial-history-project` (`--project` to change).

## 2. Copy what is already pulled (for the test deployment)

The calibration lists index into these exact files, so copy them rather than
re-pulling. From the laptop, with the T7 drive mounted:

```tcsh
rsync -av --progress "/Volumes/T7 Shield/MethaneBench/" <user>@<server>:$DATA/ --exclude co2_transfer
```

| Folder | Contents |
|---|---|
| `mars_s2l/meta/` | MARS-S2L `validated_images_all.csv` (labels + plume outlines) |
| `mars_s2l/imagery/` | 2,159 MARS target dates; `calibration_mars.txt` (45 scenes: obvious / harder / no plume) |
| `controlled_releases/imagery/` | Casa Grande 2022 overpasses; `calibration_releases.txt` (14 scenes) |
| `pi_calibration/` | the original calibration pull (`imagery_gee_compact/`, `imagery_gee_extra_compact/`) |
| `proposals/additional_sites.csv` | candidate sites for further pulls |

## 3. Test deployment

The app reads one imagery folder. To serve the MARS and release scenes
together, link both pulls into one folder and join the two lists:

```tcsh
cd $DATA
foreach s ( sentinel2 landsat_89 )
  mkdir -p imagery_ref/$s
  ln -s $DATA/mars_s2l/imagery/normalized/$s/*.nc imagery_ref/$s/
  ln -s $DATA/controlled_releases/imagery/normalized/$s/*.nc imagery_ref/$s/
end
cat mars_s2l/imagery/calibration_mars.txt controlled_releases/imagery/calibration_releases.txt > imagery_ref/calibration_ref.txt

cd /path/to/MethaneBench_Labeler
setenv IMAGERY_PATH $DATA/imagery_ref
setenv CALIBRATION_FILE $DATA/imagery_ref/calibration_ref.txt
setenv DB_PATH ./test_labels.db
setenv MPLCONFIGDIR /tmp
pixi run uvicorn app:app --host 127.0.0.1 --port 8001
```

Then forward port 8001 as in section 2 above. The same rules apply: never
port 63951, keep `--host 127.0.0.1`.

## 4. Fuller pull

Run long pulls in `tmux` or `screen`. Re-running a command resumes it, skipping
sites already complete. Files are written compact (int16); the MARS pull averaged
about 12 MB per site, sensor and year.

```tcsh
# MARS-S2L: more labelled dates per site and sensor (default 2 + 2)
pixi run python mars_targets.py $DATA/mars_s2l/meta/validated_images_all.csv \
    --k-plume 6 --k-clear 6 --out $DATA/mars_s2l/targets_full
pixi run python gee_fetch.py --calibration $DATA/mars_s2l/targets_full/mars_targets.txt \
    --all --out $DATA/mars_s2l/imagery_full

# Calibration lists for the new pull (acq_idx changes with each pull)
pixi run python select_calibration.py $DATA/mars_s2l/imagery_full \
    --out $DATA/mars_s2l/imagery_full/calibration_mars.txt
```

Check free space (`df -h $DATA`) before the MARS pull: the 2 + 2 pull was
9.2 GB, and each extra target year adds background for that site and sensor.
