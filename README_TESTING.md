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
  <http://localhost:8001> locally.

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