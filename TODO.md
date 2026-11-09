# MethaneBench labeler: to-do

## On the server

- [ ] Set up `gee-imagery` there and log in to Earth Engine (README_TESTING.md, "GEE imagery and reference scenes on the server")
- [ ] Copy the T7 drive pulls (MARS-S2L, controlled releases, PI calibration) and run the test deployment
- [ ] Fuller MARS-S2L pull (more labelled dates per site), then rerun `select_calibration.py` on it
- [ ] Pull more reference detections, so the calibration set does not rest on MARS-S2L alone:
  - [ ] He et al. 2024, PNAS: 776 Landsat 5 plumes in Turkmenistan, 1986–2011 ([Dryad](https://datadryad.org/stash/dataset/doi:10.5061/dryad.4mw6m90hp)); covers the PI's Landsat 4/5 scenes
  - [ ] Ehret et al. 2022, ES&T: ~1,200 Sentinel-2 events, Nov 2017 – Jun 2021 ([arXiv:2110.11832](https://arxiv.org/abs/2110.11832)); find the data link
  - [ ] Irakulis-Loitxate et al. 2022, ES&T: 29 west Turkmenistan emitters, Sentinel-2/Landsat 2017–2020; transcribe dates from the paper and supplement
  - [ ] Varon et al. 2021, AMT: dated Sentinel-2 detections at Hassi Messaoud and Korpeje; transcribe from the paper tables
  - [ ] Ehrenberg AZ 2021 controlled releases (Sherwin et al. 2023, Sci Rep): pull overpasses, get release rates
  - [ ] MethaneS2CM (Hugging Face, CC BY-NC): Carbon Mapper plumes paired with Sentinel-2; use as candidate sites, not answers (plumes may not show in Sentinel-2)
  - Not using: Rouet-Leduc & Hulbert 2024 (results in doubt), CH4Net (no coordinates or dates), S2MetNet (simulated plumes)

## Calibration set

- [ ] Rebuild the obvious / harder / no-plume tiers so each samples across sources, not mostly MARS-S2L
- [ ] Decide with the PI: original literature-site list, reference-based lists, or a mix
- [ ] Decide whether the PI's sites with no dated labels stay as practice scenes
- [ ] Tell the PI: several "negative example" CH4Net sites have MARS-confirmed plumes (T21: 96, T23: 28, T8: 8, T4: 5, T2: 4, T13: 1)
- [ ] Identify the 2024 controlled-release site in MARS-S2L (41.28°N, 110.93°W; no plumes)
- [ ] Dated labels for coal and landfill sites (Shanxi, Kuzbass, Casablanca)

## Labeler

- [ ] Commit and open a PR for `gee-imagery` (never push to main)
- [ ] Scene-wide shifts in the Methane layer (~25% of scenes off by > 1σ); median centring on hold
- [ ] Tutorial images on the instructions page are placeholders
- [ ] Gap beside the thumbnails (cosmetic)
- [ ] Memory use with many large files (lazy loading)
- [ ] Labeler scoring against reference masks and releases (later)

## Data and admin

- [ ] Licences with the PI: MARS-S2L CC BY-NC-SA 4.0, Stanford releases CC BY 4.0, MethaneS2CM CC BY-NC 4.0
- [ ] Laptop storage: swap in the compact `imagery_gee*` folders, delete raw copies, the truncated `imagery_20260613.tar.gz` and the empty `imagery_gee_extra_landsat/` (needs OK)
- [ ] Commit the EDA scripts on `gee-eda` in MethaneBench_EDA (staged, not committed)
- [ ] Methods doc: add the new reference sources once pulled; verify the references marked "to verify"
