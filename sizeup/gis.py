"""County GIS layers for the CAD maps (GitHub #91): Westchester County GIS open data (tax parcels,
municipal boundaries, fire districts, hydrants, facilities, flood zones, stations ...) as map layers,
through one generic, config-driven ArcGIS feature-layer mechanism.

User, 2026-10-13: "add Westchester County GIS open-data datasets as map layers ... there are a lot of
data points, let's focus on the important ones for fire service".

Source: Westchester County GIS (https://gis.westchestercountyny.gov, an ArcGIS Hub; the services at
https://giswww.westchestergov.com/wcgis/rest/services/...). Every dataset is one ArcGIS REST layer
(.../MapServer/<n> or .../FeatureServer/<n>) and one entry in [gis] layers (sizeup/config.py; the
starter set is DEFAULT_LAYERS below): its key, label, group, the layer's REST URL, the style (colors
as design tokens), the zoom it shows from, the fields shown on a click, how often it is fetched, and
a bbox or a where clause to keep only our area. Adding a dataset is one config entry, no code.

Terms: each dataset's licence is recorded on its entry (licence) and in docs/cloud.md "County GIS
layers"; the county asks for attribution, which the maps give ("Westchester County GIS" on the
Layers panel and in the map's attribution). Only datasets whose terms allow reuse are configured.

How it works: the hub (or all-in-one; never a station, which has no maps) fetches each layer with the
ArcGIS REST query API, f=geojson, paged with resultOffset (never more than the service's
maxRecordCount a page), clipped to the layer's bbox (the county is big; our area is small), the
geometry rounded to 6 decimals and simplified (Douglas-Peucker, simplify_m metres) and only the
configured fields kept; the result is cached on disk (state dir, gis/<key>.geojson) so a restart
fetches nothing, and fetched again on the layer's schedule (daily / weekly / monthly) with a backoff
after a failure (5 min doubling to 6 h). A User-Agent names sizeup. The browser never talks to the
county: it gets everything from the hub.

HTTP (sizeup/web/server.py; any signed-in person, viewers too; GET only):
    GET /api/cad/gis                 the catalog: {configured, source, attribution, link, rev, layers: [{key, label,
                                     group, kind, style, minzoom, fields, on, refresh, tiled, at, n, bytes, error,
                                     updated, licence, page}]}
    GET /api/cad/gis/<key>           the layer as GeoJSON (gzip when accepted; ETag / If-None-Match -> 304)
    GET /api/cad/gis/<key>?bbox=w,s,e,n   only the features touching that box: how a tiled layer (the parcels:
                                     too big to send whole) is served, the browser asking for its viewport
                                     at the layer's minzoom and up; the box may be at most TILE_MAX_DEG a side
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen

SOURCE = "Westchester County GIS"
ATTRIBUTION = "Westchester County GIS"
LINK_URL = "https://gis.westchestercountyny.gov/"
TERMS_URL = "https://gis.westchestercountyny.gov/"     # the disclaimer is on the portal's home page (no terms page)
try:                                   # the shared User-Agent (sizeup/__init__.py), when it has landed
    from sizeup import USER_AGENT as UA
except ImportError:
    from sizeup import __version__
    UA = f"sizeup/{__version__} (Port Chester FD dispatch; contact pcfd-sizeup.cqmpy@dralias.com)"
REFRESH_S = {"hourly": 3600.0, "daily": 86400.0, "weekly": 7 * 86400.0, "monthly": 30 * 86400.0}
BACKOFF_S = (300.0, 6 * 3600.0)     # a failed fetch: 5 min doubling to 6 h
TIMEOUT_S = 60.0
MAX_PAGE_BYTES = 64 * 1024 * 1024
PAGE = 1000                         # features a query at most (less when the service's maxRecordCount is less)
MAX_FEATURES = 250_000              # a runaway layer stops here
TILE_MAX_DEG = 0.25                 # a bbox query at most this many degrees a side (zoom 13 on a wide screen)
TILE_TTL_S = 60.0
LAYER_TTL_S = 300.0
KINDS = ("point", "line", "polygon")
KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
LAYER_PATH = re.compile(r"/api/cad/gis/([a-z][a-z0-9_]{0,31})")
URL_RE = re.compile(r"https://[^/\s?#]+/\S*/(FeatureServer|MapServer)/\d+")
JSON = "application/json"
# West, south, east, north: Port Chester, Rye Brook, Rye, Harrison, Mamaroneck and White Plains' east
# edge (the county data stops at the state line: Greenwich is not in it).
DEFAULT_BBOX = (-73.80, 40.92, -73.56, 41.10)
# The parcels: only Port Chester, Rye Brook, Rye and Harrison, with a margin (the issue: "parcels only
# at zoom 15+ and only for Port Chester, Rye Brook, Rye, Harrison, Greenwich edge").
PARCELS_BBOX = (-73.77, 40.94, -73.63, 41.07)
GEOM_KIND = {"esriGeometryPoint": "point", "esriGeometryMultipoint": "point", "esriGeometryPolyline": "line",
             "esriGeometryPolygon": "polygon"}


@dataclass(frozen=True)
class Layer:
    """One dataset: a [gis] layers entry (sizeup/config.py _gis validates one)."""
    key: str                          # "parcels": the toggle's id, the file name, the API path
    label: str                        # "Tax parcels"
    url: str                          # the ArcGIS REST layer: https://.../MapServer/12 or .../FeatureServer/0
    group: str = "County GIS"         # the sub-heading on the Layers panel ("Boundaries", "Utilities", ...)
    kind: str = ""                    # point | line | polygon; "" = the service's geometryType
    style: dict = field(default_factory=dict)   # color (a token "--info" or "#hex"), fill (opacity), width, radius
    minzoom: int = 0                  # drawn from this zoom (the panel says "zoom in" below it)
    fields: tuple[tuple[str, str], ...] = ()   # (the service's field, the label shown), in order; the first is the title
    refresh: str = "daily"            # hourly | daily | weekly | monthly
    bbox: tuple[float, float, float, float] | None = None   # None: the [gis] bbox
    where: str = ""                   # an extra SQL where for the query ("MUNI IN ('Rye', ...)")
    on: bool = False                  # on for a viewer who has never chosen
    tiled: bool = False               # served by bbox (the browser asks for its viewport), not whole
    simplify_m: float = 0.0           # Douglas-Peucker tolerance in metres (0: as the county drew it)
    licence: str = ""                 # the dataset's terms, as its portal page states them
    page: str = ""                    # its page on the portal
    enabled: bool = True
    asof: str = ""                    # the data's date as the county states it ("as of Oct 2015"): shown
    ours_field: str = ""              # the field naming the department: features matching ours are marked _ours
    drop_ours: bool = False           # leave out our own departments' features: the map draws ours from our own data
                                      # (the county's 2015 fire stations put a "PORT CHESTER F.D." at Don Bosco Pl, 2026-10-14)
    note: str = ""                    # what the data doesn't say, shown with every feature ("catenary: 12.5 kV")
    icon: str = ""                    # the mark for a point layer (#111): static/icons.svg "i-gis-<icon>" ("hydrant",
                                      # "fire-station"), in the style's color on the map, by its toggle, in the click box; "": a dot
    sub: str = ""                     # what a feature is, under its title in the click box ("Petroleum bulk storage (NYSDEC PBS)");
                                      # "": the label

    @property
    def every_s(self) -> float:
        return REFRESH_S[self.refresh]

    def public(self) -> dict:
        return {"key": self.key, "label": self.label, "group": self.group, "kind": self.kind, "style": dict(self.style),
                "minzoom": self.minzoom, "fields": [list(f) for f in self.fields], "on": self.on, "refresh": self.refresh,
                "tiled": self.tiled, "licence": self.licence, "page": self.page, "asof": self.asof,
                "ours_field": self.ours_field, "drop_ours": self.drop_ours, "note": self.note, "icon": self.icon, "sub": self.sub}


# ---- the starter set: docs/cloud.md "County GIS layers" has each dataset's source and terms -------------------
#
# Surveyed 2026-10-13. Every county dataset is public on the portal under the county's disclaimer
# (the home page: "provided 'as is' to the public for general information, ... not accurate to surveying
# or engineering standards, and not suitable for site-specific purposes"; no restriction on reuse); the
# parcels add NYS ITS's "as is" terms. Each entry's licence names which. The services:
_WC = "https://giswww.westchestergov.com/arcgis/rest/services"
_HUB = "https://gis.westchestercountyny.gov/datasets/"
_AGOL = "https://services.arcgis.com/XKEHpOulfycN9cGC/arcgis/rest/services"
COUNTY_TERMS = "Westchester County GIS disclaimer: public, 'as is', not survey-accurate, no site-specific use; no reuse restriction"
NYS_TERMS = ("NYS ITS parcel terms: 'as is', no warranty; Westchester County: general reference only, tax maps of "
             "record are the assessor's; no reuse restriction")
OUR_TOWNS = "MUNI_NAME IN ('Port Chester','Rye Brook','Rye','Harrison')"

DEFAULT_LAYERS: tuple[Layer, ...] = (
    # -- Parcels ---------------------------------------------------------------------------------------------
    Layer("parcels", "Tax parcels", f"{_WC}/DataHub_TaxParcels/MapServer/0", group="Parcels", kind="polygon",
          style={"color": "--dim", "fill": 0.06, "width": 0.8, "label": "PARCEL_ADDR", "labelzoom": 17},
          minzoom=15, tiled=True, refresh="weekly", bbox=PARCELS_BBOX, where=OUR_TOWNS, simplify_m=0.5,
          fields=(("PARCEL_ADDR", "Address"), ("LOC_UNIT", "Unit"), ("MUNI_NAME", "Municipality"), ("PRIMARY_OWNER", "Owner"),
                  ("ADD_OWNER", "Owner (also)"), ("PROP_CLASS", "Class (NYS code)"), ("ACRES", "Acres"), ("SQFT_LIVING", "Living sq ft"),
                  ("YR_BLT", "Year built"), ("BLDG_STYLE_DESC", "Style"), ("HEAT_TYPE_DESC", "Heat"), ("FUEL_TYPE_DESC", "Fuel"),
                  ("PRINT_KEY", "Tax map")),
          licence=NYS_TERMS, page=f"{_HUB}02e7f9092db64c54adc745653dd195fc_0", asof="2025 roll, May 2026"),
    # -- Boundaries ------------------------------------------------------------------------------------------
    Layer("fire_districts", "Fire districts", f"{_WC}/Datahub_Boundaries/MapServer/153", group="Boundaries", kind="polygon",
          style={"color": "--err", "fill": 0.04, "width": 1.6, "label": "FIRENAME", "labelzoom": 12}, on=True, refresh="monthly", simplify_m=1.0,
          fields=(("FIRENAME", "District"), ("FIREID", "District no.")), ours_field="FIRENAME",
          licence=COUNTY_TERMS, page=f"{_HUB}9787aa03db7b464e88a9ecbf15a653c0_153", asof="as of Feb 2011",
          note="The county's fire agency boundaries; Rye Brook is inside the Port Chester FD district here"),
    Layer("ems_districts", "EMS districts", f"{_WC}/Datahub_Boundaries/MapServer/152", group="Boundaries", kind="polygon",
          style={"color": "--ok", "fill": 0.04, "width": 1.2, "dash": 1}, refresh="monthly", simplify_m=1.0,
          fields=(("EMSNAME", "District"),), licence=COUNTY_TERMS, page=f"{_HUB}8851721a78f343e08382ec21cf2f1f5c_152", asof="as of Feb 2011"),
    Layer("municipalities", "Municipal boundaries", f"{_WC}/Datahub_Boundaries/MapServer/163", group="Boundaries", kind="polygon",
          style={"color": "--text-2", "fill": 0.0, "width": 1.4, "dash": 1}, refresh="monthly", simplify_m=1.0,
          fields=(("NAME", "Municipality"), ("SQMILES", "Square miles")), licence=COUNTY_TERMS,
          page=f"{_HUB}278ce38ecd784b79993af098f81809ed_163"),
    # -- Utilities (the county's infrastructure layers) ---------------------------------------------------------------
    Layer("hydrants", "Hydrants", f"{_WC}/DataHub_Infrastructure/MapServer/147", group="Utilities", kind="point", icon="hydrant",
          style={"color": "--info", "radius": 4.5, "label": "HYD_NUMBER", "labelzoom": 17}, minzoom=14, refresh="monthly",
          fields=(("FIRENAME", "Fire district"), ("HYD_NUMBER", "Hydrant no."), ("MUNCIPALIT", "Municipality")),
          licence=COUNTY_TERMS, page=f"{_HUB}b1766f745f954027936f74e7b0d2de65_147", asof="as of Oct 2015",
          note="Positions from the departments that contributed them (some partial); no flow or size data"),
    # The county's copy of NYSDEC's PBS registry carries the facility, its address and PBS number only (no tank count,
    # product or gallons: those are in DEC's own PBS database, by the PBS number). The mark: a drum in the warning
    # color, a third larger than the hydrant's, so a fuel site is unmistakable (#111).
    Layer("petroleum", "Fuel storage sites (PBS)", f"{_WC}/DataHub_EnvironmentandPlanning/MapServer/165", group="Utilities", kind="point",
          icon="petroleum", sub="Petroleum bulk storage (NYSDEC PBS)",
          style={"color": "--warn", "radius": 5, "size": 1.35}, minzoom=12, refresh="monthly",
          fields=(("FACILITY_NAME", "Facility"), ("STREET", "Address"), ("CITY", "City"), ("PBSNUMBER", "PBS no.")),
          licence=COUNTY_TERMS, page=f"{_HUB}04c419cd0cd4467db97cc434b36654c6_165", asof="NYSDEC, as of June 2017",
          note="Fuel tanks on site: check on a fire or spill (tank count, product and gallons: DEC's PBS record by the number)"),
    Layer("tanks_towers", "Tanks, towers, antennas", f"{_WC}/DataHub_BasemapPlanimetrics/MapServer/14", group="Utilities", kind="polygon", icon="tank",
          style={"color": "--warn", "fill": 0.3, "width": 1.2, "height": "ACTUAL_HEIGHT", "height_scale": 0.3048},
          minzoom=13, refresh="monthly", simplify_m=0.3, where="FEA_CODE IN (2110, 2120, 2130)",
          fields=(("DESCRIPTION", "Structure"), ("ACTUAL_HEIGHT", "Height (ft)"), ("APPROX_ROOF_ELEVATION", "Top elevation (ft)"),
                  ("PHOTO_DATE", "Photo date")),
          licence=COUNTY_TERMS, page=f"{_HUB}d3f656cfc71b4d5d884aa31bf341bee6_14", asof="aerial photos 2000-2023 (each feature's date)"),
    Layer("bridges", "Bridges & tunnels", f"{_WC}/DataHub_BasemapPlanimetrics/MapServer/16", group="Utilities", kind="polygon",
          style={"color": "--text-2", "fill": 0.25, "width": 1.2}, minzoom=13, refresh="monthly", simplify_m=0.3,
          fields=(("DESCRIPTION", "Structure"), ("PHOTO_DATE", "Photo date")),
          licence=COUNTY_TERMS, page=f"{_HUB}50f7273fb48948d0a514b51993066846_16", asof="aerial photos 2000-2023 (each feature's date)",
          note="Footprints only: the county publishes no load or clearance limits"),
    # -- Facilities -----------------------------------------------------------------------------------------------
    Layer("fire_stations", "Fire stations", f"{_WC}/DataHub_CommunityFacilities/MapServer/183", group="Facilities", kind="point", icon="fire-station",
          style={"color": "--err", "radius": 5}, minzoom=11, refresh="monthly",
          fields=(("NAME", "Department"), ("ADDRESS", "Address"), ("CITY", "City"), ("PHONE", "Phone")), ours_field="NAME",
          drop_ours=True, licence=COUNTY_TERMS, page=f"{_HUB}7b13a73e7a564c648d95529cebc10061_183", asof="as of Dec 2015"),
    Layer("police", "Police stations", f"{_WC}/DataHub_CommunityFacilities/MapServer/167", group="Facilities", kind="point", icon="police",
          style={"color": "--info", "radius": 5}, minzoom=11, refresh="monthly",
          fields=(("NAME", "Station"), ("ADDRESS", "Address"), ("CITY", "City"), ("PHONE", "Phone")),
          licence=COUNTY_TERMS, page=f"{_HUB}939d7f4195e542faa8a6f14623caf61d_167"),
    Layer("hospitals", "Hospitals", f"{_WC}/DataHub_CommunityFacilities/MapServer/178", group="Facilities", kind="point", icon="hospital",
          style={"color": "--ok", "radius": 6}, minzoom=10, refresh="monthly",
          fields=(("HOSPNAME", "Hospital"), ("ADDRESS", "Address"), ("CITY", "City"), ("PHONE", "Phone")),
          licence=COUNTY_TERMS, page=f"{_HUB}41f2aa4e2f74435ab7c05c7d56a9bf11_178", asof="NYS DOH, as of Apr 2020"),
    Layer("nursing_homes", "Nursing homes", f"{_AGOL}/DOHdata/FeatureServer/1", group="Facilities", kind="point", icon="nursing-home",
          style={"color": "--warn", "radius": 5}, minzoom=12, refresh="monthly",
          fields=(("FACILITY_NAME", "Facility"), ("FACILITY_ADDRESS_2", "Address"), ("FACILITY_CITY", "City"), ("PHONE", "Phone"),
                  ("AVERAGE_OCCUPIED_BEDS", "Occupied beds (avg)"), ("OPERATOR", "Operator")),
          licence=COUNTY_TERMS, page=f"{_HUB}c87ffaef3c344d8086d2a9eb0db2f709_1", asof="NYS DOH, Sept 2020"),
    Layer("adult_care", "Assisted living & adult care", f"{_AGOL}/DOHdata/FeatureServer/0", group="Facilities", kind="point", icon="nursing-home",
          style={"color": "--warn", "radius": 5}, minzoom=12, refresh="monthly",
          fields=(("FACILITY_NAME", "Facility"), ("ADDRESS", "Address"), ("CITY", "City"), ("PHONE", "Phone"),
                  ("NUMBER_OF_BEDS", "Beds"), ("TYPE_", "Type"), ("OPERATOR", "Operator")),
          licence=COUNTY_TERMS, page=f"{_HUB}c87ffaef3c344d8086d2a9eb0db2f709_0", asof="NYS DOH, Sept 2020"),
    Layer("schools", "Public schools", f"{_WC}/DataHub_CommunityFacilities/MapServer/162", group="Facilities", kind="point", icon="school",
          style={"color": "--ok", "radius": 5}, minzoom=12, refresh="monthly",
          fields=(("NAME", "School"), ("ADDRESS", "Address"), ("CITY", "City"), ("DISTNAME", "District"), ("PHONE", "Phone")),
          licence=COUNTY_TERMS, page=f"{_HUB}b19db7de171a4aed8b930ca722cfe718_162"),
    Layer("worship", "Houses of worship", f"{_WC}/DataHub_CommunityFacilities/MapServer/176", group="Facilities", kind="point", icon="worship",
          style={"color": "--text-2", "radius": 4.5}, minzoom=13, refresh="monthly",
          fields=(("ORGANIZATION", "Congregation"), ("ADDRESS", "Address"), ("CITY", "City")),
          licence=COUNTY_TERMS, page=f"{_HUB}c67b77e9dc2744eeadbc82888a6f058a_176"),
    Layer("ambulance", "Volunteer ambulance corps", f"{_WC}/DataHub_CommunityFacilities/MapServer/150", group="Facilities", kind="point", icon="ambulance",
          style={"color": "--ok", "radius": 5}, minzoom=11, refresh="monthly",
          fields=(("VAC_NAME", "Corps"), ("ADDRESS", "Address"), ("CITY", "City"), ("PHONE", "Phone")),
          licence=COUNTY_TERMS, page=f"{_HUB}167e50fdae6b4d60b734e3786307067e_150", asof="as of 2016"),
    # -- Hazards ---------------------------------------------------------------------------------------------------
    Layer("flood", "Flood plains (FEMA)", f"{_WC}/DataHub_EnvironmentandPlanning/MapServer/157", group="Hazards", kind="polygon",
          style={"color": "--info", "fill": 0.16, "width": 0.8}, minzoom=11, refresh="monthly", simplify_m=1.0,
          fields=(("FLOOD", "Flood plain (year)"),), licence=COUNTY_TERMS, page=f"{_HUB}45484ea0c9274253a955bba566df30b1_157",
          note="FEMA DFIRM: 100 = the 100-year (1%) plain, 500 = the 500-year"),
    Layer("wetlands", "Wetlands", f"{_WC}/DataHub_EnvironmentandPlanning/MapServer/154", group="Hazards", kind="polygon",
          style={"color": "--ok", "fill": 0.14, "width": 0.6}, minzoom=13, refresh="monthly", simplify_m=1.0,
          fields=(("WETLAND_TYPE", "Type"), ("ATTRIBUTE", "NWI code"), ("ACRES", "Acres")),
          licence=COUNTY_TERMS, page=f"{_HUB}d30aefee6e6043369f71ec26e0743e09_154", asof="USFWS NWI, May 2021"),
    # -- Site details (the planimetrics: drawn from aerial photos) -----------------------------------------------------
    Layer("buildings", "Buildings", f"{_WC}/DataHub_BasemapPlanimetrics/MapServer/5", group="Site details", kind="polygon",
          style={"color": "--text-2", "fill": 0.18, "width": 0.6, "height": "ACTUAL_HEIGHT", "height_scale": 0.3048,
                 "label": "ACTUAL_HEIGHT", "labelzoom": 17},
          minzoom=15, tiled=True, refresh="monthly", bbox=PARCELS_BBOX, where="FEA_CODE = 2100", simplify_m=0.3,
          fields=(("ACTUAL_HEIGHT", "Height (ft)"), ("APPROX_ROOF_ELEVATION", "Roof elevation (ft)"), ("BASE_ELEVATION", "Base elevation (ft)"),
                  ("PHOTO_DATE", "Photo date")),
          licence=COUNTY_TERMS, page=f"{_HUB}bb6d28e097424a25a6ad0d33c6be3a68_5", asof="aerial photos 2000-2023 (each feature's date)",
          note="Footprint and height from photogrammetry; no storeys or use (the parcel has the style and year built)"),
    Layer("driveways", "Driveways", f"{_WC}/DataHub_BasemapPlanimetrics/MapServer/10", group="Site details", kind="polygon",
          style={"color": "--dim", "fill": 0.14, "width": 0.4}, minzoom=16, tiled=True, refresh="monthly", bbox=PARCELS_BBOX,
          simplify_m=0.3, fields=(("DESCRIPTION", "Surface"), ("PHOTO_DATE", "Photo date")),
          licence=COUNTY_TERMS, page=f"{_HUB}138dff0982dd49ee98ec7e077961b818_10", asof="aerial photos 2013-2024 (each feature's date)"),
    Layer("parking", "Parking lots", f"{_WC}/DataHub_BasemapPlanimetrics/MapServer/9", group="Site details", kind="polygon",
          style={"color": "--dim", "fill": 0.1, "width": 0.4}, minzoom=16, tiled=True, refresh="monthly", bbox=PARCELS_BBOX,
          simplify_m=0.3, fields=(("DESCRIPTION", "Surface"), ("PHOTO_DATE", "Photo date")),
          licence=COUNTY_TERMS, page=f"{_HUB}9959665f16aa494e860e8d678bb893e6_9", asof="aerial photos 2013-2024 (each feature's date)"),
    Layer("walls", "Walls & guard rails", f"{_WC}/DataHub_BasemapPlanimetrics/MapServer/12", group="Site details", kind="line",
          style={"color": "--warn", "width": 1.6}, minzoom=16, tiled=True, refresh="monthly", bbox=PARCELS_BBOX, simplify_m=0.3,
          fields=(("DESCRIPTION", "Barrier"), ("PHOTO_DATE", "Photo date")),
          licence=COUNTY_TERMS, page=f"{_HUB}94425da911474629ad5249b86102eef0_12", asof="aerial photos 2000-2023 (each feature's date)"),
    Layer("streams", "Streams", f"{_WC}/DataHub_BasemapPlanimetrics/MapServer/0", group="Site details", kind="line",
          style={"color": "--info", "width": 1.6}, minzoom=13, refresh="monthly", simplify_m=1.0,
          fields=(("NAME", "Stream"), ("DESCRIPTION", "Kind")), licence=COUNTY_TERMS, page=f"{_HUB}bd1d6bddbf3d486c8fa0e6198a21da1c_0"),
    Layer("lakes", "Lakes & ponds", f"{_WC}/DataHub_BasemapPlanimetrics/MapServer/11", group="Site details", kind="polygon",
          style={"color": "--info", "fill": 0.2, "width": 0.8, "label": "NAME", "labelzoom": 14}, minzoom=12, refresh="monthly",
          simplify_m=1.0, fields=(("NAME", "Water"), ("DESCRIPTION", "Kind")),
          licence=COUNTY_TERMS, page=f"{_HUB}e83d9eb9c1504acfb99faf8a1f3ae548_11"),
    # -- Transportation -------------------------------------------------------------------------------------------------
    Layer("rail", "Railroads", f"{_WC}/DataHub_BasemapPlanimetrics/MapServer/1", group="Transportation", kind="line",
          style={"color": "--text-2", "width": 2.4, "dash": 1}, minzoom=13, refresh="monthly", simplify_m=1.0,
          fields=(("NAME", "Line"), ("DESCRIPTION", "Kind")), licence=COUNTY_TERMS, page=f"{_HUB}7fa83627031243d999b8cd5f3ba26125_1",
          note="Metro-North New Haven Line (Port Chester, Rye): overhead catenary, 12.5 kV AC, plus the third rail at stations; "
               "the county's data names no owner or electrification"),
    Layer("train_stations", "Train stations", f"{_WC}/DataHub_Transportation/MapServer/156", group="Transportation", kind="point", icon="train",
          style={"color": "--text-2", "radius": 5.5}, minzoom=11, refresh="monthly",
          fields=(("STATNAME", "Station"),), licence=COUNTY_TERMS, page=f"{_HUB}bdf8a8439d294e3b9ef107d49b6a6743_156"),
    Layer("bus_routes", "Bee-Line bus routes", f"{_WC}/DataHub_Transportation/MapServer/154", group="Transportation", kind="line",
          style={"color": "--ok", "width": 1.8}, minzoom=12, refresh="monthly", simplify_m=2.0,
          fields=(("LINEABBR", "Route"), ("LINENAME", "Name")), licence=COUNTY_TERMS, page=f"{_HUB}7305a04a5aa24fd5957301d554f55c24_154",
          asof="Apr 2022"),
    Layer("bus_stops", "Bee-Line bus stops", f"{_WC}/DataHub_Transportation/MapServer/153", group="Transportation", kind="point", icon="bus",
          style={"color": "--ok", "radius": 3.5}, minzoom=15, refresh="monthly",
          fields=(("STOPNAME", "Stop"), ("ONSTREET", "On"), ("CROSSTREET", "At"), ("ROUTES", "Routes")),
          licence=COUNTY_TERMS, page=f"{_HUB}b041128dd5334a999f20c11ba5074366_153"),
)


# ---- geometry: rounding, bboxes, Douglas-Peucker ----------------------------------------------------------


def _dp(pts: list, tol: float) -> list:
    """Douglas-Peucker on [[x, y], ...] (x already scaled so a unit is the same distance as a unit of y);
    the ends always kept. Iterative: a coastline-long ring won't blow the stack."""
    n = len(pts)
    if n < 3 or tol <= 0:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if ll == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(pts, keep) if k]


def simplify_line(coords: list, tol_m: float, ring: bool = False) -> list:
    """A line's or ring's [lon, lat] list simplified to tol_m metres; a ring keeps at least 4 points
    (closed), a line 2."""
    if tol_m <= 0 or len(coords) < 3:
        return coords
    lat0 = sum(c[1] for c in coords) / len(coords)
    kx = math.cos(math.radians(lat0))
    tol = tol_m / 111_320.0
    out = _dp([[c[0] * kx, c[1]] for c in coords], tol)
    if ring and len(out) < 4:
        return coords
    return [[round(x / kx, 6), round(y, 6)] for x, y in out]


def _round_pt(c) -> list:
    return [round(float(c[0]), 6), round(float(c[1]), 6)]


def tidy_geometry(g: dict | None, tol_m: float = 0.0) -> dict | None:
    """The geometry rounded to 6 decimals (about 0.1 m) and simplified; None when it has nothing left."""
    if not isinstance(g, dict):
        return None
    t, c = g.get("type"), g.get("coordinates")
    try:
        if t == "Point":
            return {"type": t, "coordinates": _round_pt(c)}
        if t == "MultiPoint":
            return {"type": t, "coordinates": [_round_pt(p) for p in c]} if c else None
        if t == "LineString":
            return {"type": t, "coordinates": simplify_line([_round_pt(p) for p in c], tol_m)} if len(c) >= 2 else None
        if t == "MultiLineString":
            lines = [simplify_line([_round_pt(p) for p in ln], tol_m) for ln in c if len(ln) >= 2]
            return {"type": t, "coordinates": lines} if lines else None
        if t == "Polygon":
            rings = [simplify_line([_round_pt(p) for p in r], tol_m, ring=True) for r in c if len(r) >= 4]
            return {"type": t, "coordinates": rings} if rings else None
        if t == "MultiPolygon":
            polys = []
            for poly in c:
                rings = [simplify_line([_round_pt(p) for p in r], tol_m, ring=True) for r in poly if len(r) >= 4]
                if rings:
                    polys.append(rings)
            return {"type": t, "coordinates": polys} if polys else None
    except (TypeError, ValueError, IndexError):
        return None
    return None


def geometry_bbox(g: dict) -> tuple[float, float, float, float] | None:
    """(west, south, east, north) of a geometry."""
    pts: list = []

    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            pts.append(c)
        elif isinstance(c, (list, tuple)):
            for x in c:
                walk(x)
    walk(g.get("coordinates"))
    if not pts:
        return None
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _touches(a, b) -> bool:
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _in_ring(lon: float, lat: float, ring: list) -> bool:
    """Ray casting: the point inside the ring."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


def contains(g: dict, lat: float, lon: float) -> bool:
    """A Polygon / MultiPolygon contains the point (its holes respected)."""
    polys = g.get("coordinates") or []
    if g.get("type") == "Polygon":
        polys = [polys]
    elif g.get("type") != "MultiPolygon":
        return False
    for rings in polys:
        if rings and _in_ring(lon, lat, rings[0]) and not any(_in_ring(lon, lat, r) for r in rings[1:]):
            return True
    return False


def _dept_key(name: str) -> str:
    """"Port Chester FD" / "PORT CHESTER FIRE DEPARTMENT" / "Village of Port Chester" -> "portchester"."""
    t = re.sub(r"\b(fire|dept|department|district|fd|protection|village|town|city|of|the)\b", " ", str(name or "").lower())
    return re.sub(r"[^a-z0-9]", "", t)


def parse_bbox(text: str, max_deg: float | None = None) -> tuple[float, float, float, float]:
    """"w,s,e,n" -> the box; ValueError when it isn't one (or is bigger than max_deg a side)."""
    try:
        w, s, e, n = (float(x) for x in text.split(","))
    except ValueError:
        raise ValueError("bbox: west,south,east,north in degrees") from None
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError("bbox: west < east and south < north")
    if max_deg is not None and (e - w > max_deg or n - s > max_deg):
        raise ValueError(f"bbox: at most {max_deg} degrees a side (zoom in)")
    return (w, s, e, n)


# ---- the ArcGIS REST query --------------------------------------------------------------------------------


def query_url(url: str, bbox=None, where: str = "", fields: tuple[str, ...] = (), offset: int = 0, count: int = PAGE) -> str:
    """One page of <layer>/query as GeoJSON in WGS84, for our box, only the fields we show."""
    params = [("f", "geojson"), ("where", where or "1=1"), ("outFields", ",".join(fields) if fields else "*"),
              ("returnGeometry", "true"), ("outSR", "4326"), ("geometryPrecision", "6"),
              ("resultOffset", str(offset)), ("resultRecordCount", str(count))]
    if bbox:
        w, s, e, n = bbox
        params += [("geometry", f"{w},{s},{e},{n}"), ("geometryType", "esriGeometryEnvelope"), ("inSR", "4326"),
                   ("spatialRel", "esriSpatialRelIntersects")]
    return f"{url}/query?{urlencode(params)}"


def http_get(url: str, timeout: float, limit: int) -> tuple[int, dict, bytes]:
    """(status, headers, body): urllib, GET only. Errors as HTTPError / URLError / OSError."""
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json, application/geo+json;q=0.9, */*;q=0.5"})
    with urlopen(req, timeout=timeout) as r:
        body = r.read(limit + 1)
        if len(body) > limit:
            raise ValueError(f"more than {limit // (1024 * 1024)} MB")
        return r.status, {k.lower(): v for k, v in r.headers.items()}, body


def _json(body: bytes) -> dict:
    doc = json.loads(body.decode("utf-8-sig", errors="replace"))
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    if isinstance(doc.get("error"), dict):       # ArcGIS answers 200 with {"error": {code, message, details}}
        e = doc["error"]
        raise ValueError(f"ArcGIS error {e.get('code', '')}: {e.get('message', '')} {' '.join(e.get('details') or [])}".strip())
    return doc


def layer_info(fetch: Callable, url: str) -> dict:
    """The layer's own description (<layer>?f=json): what we keep of it."""
    code, _, body = fetch(f"{url}?f=json", TIMEOUT_S, 4 * 1024 * 1024)
    if code != 200:
        raise HTTPError(url, code, f"HTTP {code}", None, None)
    doc = _json(body)
    caps = str(doc.get("capabilities", ""))
    if "Query" not in caps:
        raise ValueError(f"the layer doesn't allow queries (capabilities: {caps or 'none'})")
    edit = (doc.get("editingInfo") or {}).get("lastEditDate")
    return {"name": str(doc.get("name", ""))[:120], "kind": GEOM_KIND.get(str(doc.get("geometryType", "")), ""),
            "max": int(doc.get("maxRecordCount") or PAGE), "updated": float(edit) / 1000.0 if edit else None,
            "fields": [str(f.get("name", "")) for f in doc.get("fields") or [] if isinstance(f, dict)],
            "copyright": str(doc.get("copyrightText", ""))[:300]}


def fetch_features(fetch: Callable, layer: Layer, bbox, page: int = PAGE) -> list[dict]:
    """Every feature of the layer in the box, page by page (resultOffset), until the service says there
    are no more (no exceededTransferLimit and a short page)."""
    fields = tuple(f for f, _ in layer.fields)
    out, offset = [], 0
    while True:
        code, _, body = fetch(query_url(layer.url, bbox, layer.where, fields, offset, page), TIMEOUT_S, MAX_PAGE_BYTES)
        if code != 200:
            raise HTTPError(layer.url, code, f"HTTP {code}", None, None)
        doc = _json(body)
        feats = [f for f in doc.get("features") or [] if isinstance(f, dict)]
        out.extend(feats)
        more = bool(doc.get("exceededTransferLimit") or (doc.get("properties") or {}).get("exceededTransferLimit"))
        if not feats or (not more and len(feats) < page) or len(out) >= MAX_FEATURES:
            return out
        offset += len(feats)


def tidy_feature(f: dict, layer: Layer) -> dict | None:
    """A feature as we keep it: only the configured fields (all when none are), the geometry rounded and
    simplified; None when it has no usable geometry."""
    g = tidy_geometry(f.get("geometry"), layer.simplify_m)
    if g is None:
        return None
    props = f.get("properties") or {}
    if layer.fields:
        props = {k: props.get(k) for k, _ in layer.fields if props.get(k) not in (None, "")}
    else:
        props = {k: v for k, v in props.items() if v not in (None, "")}
    out = {"type": "Feature", "properties": props, "geometry": g}
    if f.get("id") is not None:
        out["id"] = f["id"]
    return out


# ---- the service -------------------------------------------------------------------------------------------


class Gis:
    def __init__(self, layers=DEFAULT_LAYERS, bbox=DEFAULT_BBOX, cache_dir: str | Path | None = None,
                 fetch: Callable[[str, float, int], tuple[int, dict, bytes]] = http_get,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time, enabled: bool = True,
                 ours: tuple[str, ...] = ()):
        self.layers: dict[str, Layer] = {l.key: l for l in layers if l.enabled}
        self.bbox = tuple(float(x) for x in bbox)
        self.ours = tuple(k for k in (_dept_key(d) for d in ours) if k)   # our departments (ours_field matches)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.fetch, self.log, self.clock, self.enabled = fetch, log, clock, enabled
        self._lock = threading.Lock()
        self.data: dict[str, list[dict]] = {k: [] for k in self.layers}
        self._boxes: dict[str, list] = {k: [] for k in self.layers}
        self.status: dict[str, dict] = {k: {"at": None, "n": 0, "bytes": 0, "error": "", "next": 0.0, "fails": 0,
                                            "updated": None, "kind": "", "rev": 0} for k in self.layers}
        self.rev = 0
        self._bytes: dict[str, tuple[int, bytes, bytes]] = {}     # key -> (rev, json, gzip) of the whole layer
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    @classmethod
    def from_config(cls, cfg, log: Callable[[str], None] = print) -> Gis:
        state = Path(cfg.cad_db).parent if cfg.cad_db else None
        return cls(cfg.gis_layers, cfg.gis_bbox, (state / "gis") if state else None, log=log, enabled=cfg.gis_enabled,
                   ours=tuple(cfg.cad_monitor_departments))

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.layers)

    def layer_bbox(self, layer: Layer):
        return layer.bbox or self.bbox

    def kind(self, key: str) -> str:
        return self.layers[key].kind or self.status[key]["kind"] or "point"

    def is_ours(self, layer: Layer, props: dict) -> bool:
        """The feature names one of our departments (ours_field; "Port Chester FD" matches "PORT CHESTER")."""
        if not layer.ours_field or not self.ours:
            return False
        k = _dept_key(props.get(layer.ours_field, ""))
        return bool(k) and any(k == o or k.startswith(o) for o in self.ours)     # "PORT CHESTER FD" is ours; "RYE FD" is not Rye Brook

    def feature_at(self, key: str, lat: float, lon: float) -> dict | None:
        """The first polygon of the layer that contains the point (its properties), else None."""
        if key not in self.layers:
            return None
        with self._lock:
            pairs = list(zip(self.data[key], self._boxes[key]))
        for f, b in pairs:
            if b and b[0] <= lon <= b[2] and b[1] <= lat <= b[3] and contains(f["geometry"], lat, lon):
                return f["properties"]
        return None

    def district_at(self, lat: float, lon: float) -> dict | None:
        """The fire district the point is in ({name, ours, ...} as the county names it), for the "is this
        ours" checks a later issue wires in (the "fire_districts" layer), else None."""
        p = self.feature_at("fire_districts", lat, lon)
        return p

    # -- the schedule ------------------------------------------------------------------------------------

    def start(self) -> Gis:
        if not self.configured:
            self.log("gis: no county GIS layers configured ([gis]); the County GIS group is off")
            return self
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="gis-county", daemon=True)
            self._thread.start()
            self.log(f"gis: {len(self.layers)} county GIS layer(s): " + ", ".join(
                f"{k} {self.layers[k].refresh}" for k in self.layers) + f"; area {self.bbox}")
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            for key in self.due():
                if self._stop.is_set():
                    break
                self.refresh(key)
            self._stop.wait(30.0)

    def due(self) -> list[str]:
        now = self.clock()
        return [k for k in self.layers if self.configured and now >= self.status[k]["next"]]

    def refresh(self, key: str) -> bool:
        """Fetch one layer now; True when it was read. Never raises."""
        layer = self.layers.get(key)
        if layer is None or not self.enabled:
            return False
        st, now = self.status[key], self.clock()
        try:
            info = layer_info(self.fetch, layer.url)
            raw = fetch_features(self.fetch, layer, self.layer_bbox(layer), min(PAGE, max(1, info["max"])))
            feats, seen = [], set()
            for f in raw:
                t = tidy_feature(f, layer)
                if t is None:
                    continue
                if self.is_ours(layer, t["properties"]):
                    if layer.drop_ours:
                        continue
                    t["properties"]["_ours"] = True
                fid = t.get("id")
                if fid is not None:
                    if fid in seen:
                        continue
                    seen.add(fid)
                feats.append(t)
            boxes = [geometry_bbox(f["geometry"]) for f in feats]
            body = json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")).encode()
        except HTTPError as e:
            return self._failed(key, now, f"HTTP {e.code}")
        except (URLError, OSError, ValueError) as e:
            return self._failed(key, now, f"{type(e).__name__}: {getattr(e, 'reason', e)}")
        except Exception as e:  # a surprise in one layer must not stop the others
            return self._failed(key, now, f"{type(e).__name__}: {e}")
        with self._lock:
            self.data[key], self._boxes[key] = feats, boxes
            st.update(at=now, n=len(feats), bytes=len(body), error="", fails=0, next=now + layer.every_s,
                      updated=info["updated"], kind=info["kind"] or st["kind"])
            self._changed(key)
        self._save(key, feats, info)
        gz = len(gzip.compress(body, 6))
        self.log(f"gis: {key}: {len(feats)} features, {len(body) / 1e6:.2f} MB ({gz / 1e6:.2f} MB gzip), "
                 f"next in {layer.every_s / 3600:.0f} h")
        return True

    def _failed(self, key: str, now: float, why: str) -> bool:
        st = self.status[key]
        st["fails"] += 1
        wait = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** (st["fails"] - 1))
        with self._lock:
            changed = st["error"] != why
            st.update(error=why[:300], next=now + wait)
            if changed:
                self.rev += 1
        self.log(f"gis: {key}: {why}; next try in {wait / 60:.0f} min")
        return False

    def _changed(self, key: str) -> None:
        self.rev += 1
        self.status[key]["rev"] = self.rev
        self._bytes.pop(key, None)

    # -- the disk cache --------------------------------------------------------------------------------------

    def _signature(self, layer: Layer) -> str:
        """What a cached file must have been fetched with to still be this layer's."""
        doc = {"url": layer.url, "bbox": [round(x, 4) for x in self.layer_bbox(layer)], "where": layer.where,
               "fields": [f for f, _ in layer.fields], "simplify_m": layer.simplify_m}
        if layer.drop_ours:                               # which features were left out is part of the cache
            doc["drop_ours"] = sorted(self.ours)
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]

    def _path(self, key: str) -> Path | None:
        return (self.cache_dir / f"{key}.geojson") if self.cache_dir else None

    def _load(self) -> None:
        for key, layer in self.layers.items():
            p = self._path(key)
            if p is None or not p.is_file():
                continue
            try:
                doc = json.loads(p.read_text())
                meta = doc.get("sizeup") or {}
                feats = doc.get("features")
                at = meta.get("at")
                if meta.get("signature") != self._signature(layer) or not isinstance(feats, list) or not isinstance(at, (int, float)):
                    continue                              # fetched for another config: fetch afresh
                if at > self.clock() + 60:
                    continue
                self.data[key] = feats
                self._boxes[key] = [geometry_bbox(f["geometry"]) for f in feats]
                self.status[key].update(at=at, n=len(feats), next=at + layer.every_s, updated=meta.get("updated"),
                                        kind=meta.get("kind", ""), bytes=meta.get("bytes", 0))
                self._changed(key)
            except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
                self.log(f"gis: cache {p.name} not read ({type(e).__name__}); fetching afresh")

    def _save(self, key: str, feats: list[dict], info: dict) -> None:
        p = self._path(key)
        if p is None:
            return
        st = self.status[key]
        doc = {"type": "FeatureCollection", "features": feats,
               "sizeup": {"source": SOURCE, "at": st["at"], "signature": self._signature(self.layers[key]),
                          "updated": info.get("updated"), "kind": info.get("kind", ""), "name": info.get("name", ""),
                          "bytes": st["bytes"]}}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(p)
        except OSError as e:
            self.log(f"gis: cache {p.name} not saved ({type(e).__name__}: {e})")

    # -- what the pages get -----------------------------------------------------------------------------------

    def public(self) -> dict:
        with self._lock:
            layers = []
            for key, layer in self.layers.items():
                st = self.status[key]
                layers.append(layer.public() | {"kind": self.kind(key), "at": st["at"], "n": st["n"], "bytes": st["bytes"],
                                                "error": st["error"], "updated": st["updated"], "rev": st["rev"]})
            return {"configured": self.configured, "source": SOURCE, "attribution": ATTRIBUTION, "link": LINK_URL,
                    "terms": TERMS_URL, "rev": self.rev, "bbox": list(self.bbox), "layers": layers}

    def _layer_bytes(self, key: str) -> tuple[int, bytes, bytes]:
        with self._lock:
            cur = self._bytes.get(key)
            if cur is not None and cur[0] == self.status[key]["rev"]:
                return cur
            rev, feats = self.status[key]["rev"], list(self.data[key])
        body = json.dumps({"type": "FeatureCollection", "key": key, "rev": rev, "features": feats}, separators=(",", ":")).encode()
        cur = (rev, body, gzip.compress(body, 6))
        with self._lock:
            self._bytes[key] = cur
        return cur

    def features(self, key: str) -> list[dict]:
        """The layer's features as loaded ([] when it isn't, or isn't configured)."""
        with self._lock:
            return list(self.data.get(key) or [])

    def features_in(self, key: str, bbox) -> list[dict]:
        with self._lock:
            return [f for f, b in zip(self.data[key], self._boxes[key]) if b and _touches(b, bbox)]

    # -- HTTP -----------------------------------------------------------------------------------------------------

    def handles(self, path: str) -> bool:
        return path == "/api/cad/gis" or bool(LAYER_PATH.fullmatch(path))

    def get(self, path: str, query: str, headers=None) -> tuple[int, bytes, str, dict]:
        """(code, body, content type, headers). headers: the request's (If-None-Match, Accept-Encoding)."""
        if path == "/api/cad/gis":
            return 200, json.dumps(self.public(), separators=(",", ":")).encode(), JSON, {}
        m = LAYER_PATH.fullmatch(path)
        key = m.group(1) if m else ""
        if key not in self.layers:
            return 404, b'{"error":"no such layer"}', JSON, {}
        q = parse_qs(query or "")
        bbox_s = (q.get("bbox") or [""])[0].strip()
        layer, st = self.layers[key], self.status[key]
        if layer.tiled and not bbox_s:
            return 400, b'{"error":"this layer is served by viewport: add ?bbox=west,south,east,north"}', JSON, {}
        if bbox_s:
            try:
                bbox = parse_bbox(bbox_s, TILE_MAX_DEG)
            except ValueError as e:
                return 400, json.dumps({"error": str(e)}).encode(), JSON, {}
            feats = self.features_in(key, bbox)
            body = json.dumps({"type": "FeatureCollection", "key": key, "rev": st["rev"], "bbox": list(bbox), "features": feats},
                              separators=(",", ":")).encode()
            etag = f'W/"{key}-{st["rev"]}-{hashlib.sha1(bbox_s.encode()).hexdigest()[:10]}"'
            ttl, gz = TILE_TTL_S, None
        else:
            rev, body, gz = self._layer_bytes(key)
            etag, ttl = f'W/"{key}-{rev}"', LAYER_TTL_S
        h = {"ETag": etag, "Cache-Control": f"private, max-age={int(ttl)}", "Vary": "Accept-Encoding"}
        inm = _header(headers, "If-None-Match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            return 304, b"", JSON, h
        if "gzip" in _header(headers, "Accept-Encoding").lower():
            body = gz if gz is not None else gzip.compress(body, 6)
            h["Content-Encoding"] = "gzip"
        return 200, body, "application/geo+json", h


def _header(headers, name: str) -> str:
    if headers is None:
        return ""
    try:
        return str(headers.get(name) or "")
    except AttributeError:
        return ""
