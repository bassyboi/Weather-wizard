# OrdCast data-source register

Checked 12 August 2026. Confirm current terms before production use.

| Role | Source | Coverage / cadence | Operational rule |
|---|---|---|---|
| Local truth | [Kununurra Aero 002056](https://www.bom.gov.au/climate/averages/tables/cw_002056.shtml) | 1971–present; live AWS about every 10 min | Archive observations with receipt time; add calibrated farm stations. |
| Long local climate | [SILO](https://www.longpaddock.qld.gov.au/silo/) | Australian daily data, 1889–yesterday, station and 0.05° grids | CC BY 4.0; snapshot revisions; interpolated values are not raw observations. |
| Satellite | [NOAA Himawari archive](https://registry.opendata.aws/noaa-himawari/) | Native AHI L1b, 2015–present, 10 min | Open archive with attribution; retain source issue/receipt time. |
| Radar | [Bureau WA radar inventory](https://www.bom.gov.au/australia/radar/wa_radar_sites_table.shtml) | Wyndham radar, roughly 70 km from Kununurra | Prototype motion only until historical/QC/licensing access is settled. |
| Global ensemble | [NOAA GEFS](https://www.nco.ncep.noaa.gov/pmb/products/gens/) | Current operational guidance uses 31 members; product subsets and archive holdings vary | Record exact product/member IDs and archive original issued cycles and publication latency. |
| Reforecast | [GEFSv12 reforecast](https://registry.opendata.aws/noaa-gefs-reforecast/) | 2000–2019, daily 5-member plus weekly 11-member runs | Initial rolling-hindcast backbone; never treat members as independent cases. |
| Global ensemble | [ECMWF open data](https://www.ecmwf.int/en/forecasts/datasets/open-data) | IFS/AIFS, four cycles/day, to 15 d | CC BY 4.0; free endpoint retains only recent runs, so archive immediately. |
| Reanalysis | [Bureau BARRA2](https://www.bom.gov.au/government-and-industry/research-and-development/research-and-development-projects/atmospheric-reanalysis) | 1979–present, 12 km regional / 4.4 km Australia | Context/labels only; future-valid analysis is not an issued forecast feature. |
| Reanalysis | [ERA5](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels) | 1940–present, hourly, 0.25° | Context/labels only; track preliminary ERA5T revisions. |
| Rain verification | [NASA IMERG](https://gpm.nasa.gov/data/imerg) | 0.1°, 30 min | Final Run for research labels; Early/Late only where latency matches operations. |
| ENSO | [NOAA CPC RONI](https://cpc.ncep.noaa.gov/products/analysis_monitoring/enso/roni/) | Monthly, 1950–present | Store vintage because recent values revise. |
| IOD | [NOAA PSL DMI](https://psl.noaa.gov/data/timeseries/month/DMI/) | Monthly, 1870–near-present | Use continuous value; calculate a timely version when monthly series lags. |
| MJO | [Bureau RMM](https://www.bom.gov.au/climate/mjo/) | Daily, 1974–real time | Use continuous RMM1/RMM2 and flag the 2014 method change. |

## Bureau access constraint

The Bureau's anonymous/public feeds do not carry a general commercial redistribution
right and have no service guarantee. ACCESS model grids, quality-controlled radar,
native satellite products, and machine-readable forecast services can be registered
subscriber products. Select the product and licence deliberately rather than scraping
public web pages into an operational service.
