# C:/Users/jaco.dewit/AppData/Local/miniforge3 python3
# -*- coding: utf-8 -*-

"""
Module Name: Download_sentinel_jp.py
Author: Jaco de Wit
Date: 2026-09-23
Description:    A robust script designed to access CDSE .SAFE Sentinel-2 archive files.
                Scan online cloud for Sentinel-2 products in date range and within shapefile.
                Downloads the 2 least cloud cover days or images for each tile within shapefile boundary.
Usage:  Change section #1 variables to reflect directory and input of the station data, and the output filename of cleaned data.
"""

##

import os
os.environ['GDAL_DATA'] = r'C:\Users\{username}\AppData\Local\miniforge3\envs\jupyterlab\Library\share\gdal'   # Only necessary if working in virtual environment and gdal .dll's get mixed up and wonky.
import re
import json
import time
import getpass
from pathlib import Path
from datetime import date, timedelta, datetime
from urllib.parse import quote

import requests
import geopandas as gpd

from shapely.geometry import box, shape
from requests.exceptions import RequestException


# ============================================================
# 1. USER SETTINGS
# ============================================================

CATALOGUE_URL = (
    "https://catalogue.dataspace.copernicus.eu/odata/v1"
)

DOWNLOAD_URL = (
    "https://download.dataspace.copernicus.eu/odata/v1"
)

TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/"
    "realms/CDSE/protocol/openid-connect/token"
)


# ------------------------------------------------------------
# CDSE ACCOUNT
# ------------------------------------------------------------
# Enter your CDSE password locally.
# Do not share or upload a script containing your password.

CDSE_USERNAME = "email@address"
CDSE_PASSWORD = "password"

# ------------------------------------------------------------
# STUDY AREA
# ------------------------------------------------------------

SHAPEFILE = (
    r"C:\Work\shapefiles\province.shp"
)


# ------------------------------------------------------------
# DATE RANGE
# ------------------------------------------------------------

START_DATE = "2025-05-01"
END_DATE = "2025-05-31"


# ============================================================
# ADAPTIVE MONTHLY ACQUISITION SETTINGS
# ============================================================

IMAGES_PER_TILE = 4

# Consider all available cloud-cover percentages
MAX_CLOUD_COVER = None

# Prevent multiple selections on the same date
REQUIRE_DIFFERENT_DATES = True

# Preferred minimum separation between acquisitions
MIN_DAYS_BETWEEN_ACQUISITIONS = 5

# Test catalogue selection before downloading
DRY_RUN = False

# ------------------------------------------------------------
# ORIGINAL SENTINEL-2 LEVEL-2A FILES
# ------------------------------------------------------------

BANDS = {
    "B03": "10m",
    "B08": "10m",
    "B8A": "20m",
    "B11": "20m",
    "SCL": "20m",   # NEW: Scene Classification Layer
}


# ------------------------------------------------------------
# OUTPUT FOLDER
# ------------------------------------------------------------

OUTPUT_DIR = (
    Path(r"C:\Work\Sentinel\Sentinel2_Native")
    / Path(SHAPEFILE).stem
    / START_DATE
)


# ------------------------------------------------------------
# DOWNLOAD SETTINGS
# ------------------------------------------------------------

# Set to 1 to test a single product.
# Set to None to download all matching products.
MAX_PRODUCTS = None

RETRIES = 5
TIMEOUT = (30, 180)

# Existing nonempty files are skipped.
SKIP_EXISTING = True

# Download available XML and GML metadata.
DOWNLOAD_METADATA = True


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

session = requests.Session()

##
# ============================================================
# 2. HTTP REQUESTS WITH RETRIES
# ============================================================

def request_with_retry(method, url, **kwargs):

    for attempt in range(1, RETRIES + 1):

        try:

            response = session.request(
                method,
                url,
                timeout=TIMEOUT,
                **kwargs,
            )

            if response.status_code in (
                429, 500, 502, 503, 504
            ):

                if attempt < RETRIES:

                    response.close()

                    wait = min(
                        15 * 2 ** (attempt - 1),
                        120,
                    )

                    print(
                        f"Server busy. Retrying in {wait}s...",
                        flush=True,
                    )

                    time.sleep(wait)
                    continue

            response.raise_for_status()

            return response

        except RequestException:

            if attempt == RETRIES:
                raise

            wait = min(
                15 * 2 ** (attempt - 1),
                120,
            )

            print(
                f"Connection issue. Retrying in {wait}s...",
                flush=True,
            )

            time.sleep(wait)


# ============================================================
# 3. CDSE AUTHENTICATION
# ============================================================

class CDSEAuth:

    def __init__(self):

        self.access_token = None
        self.refresh_token = None
        self.expires_at = 0

    def update_tokens(self, data):

        self.access_token = data["access_token"]

        self.refresh_token = data.get(
            "refresh_token",
            self.refresh_token,
        )

        self.expires_at = (
            time.time()
            + int(data.get("expires_in", 300))
            - 60
        )

    def login(self):

        print(
            "\nAuthenticating with CDSE...",
            flush=True,
        )

        response = session.post(
            TOKEN_URL,
            data={
                "client_id": "cdse-public",
                "grant_type": "password",
                "username": CDSE_USERNAME,
                "password": CDSE_PASSWORD,
            },
            timeout=(15, 30),
        )

        if not response.ok:

            try:

                error = response.json()

                error_code = error.get(
                    "error",
                    "Unknown",
                )

                description = error.get(
                    "error_description",
                    "No description provided",
                )

            except ValueError:

                error_code = (
                    f"HTTP {response.status_code}"
                )

                description = (
                    "Authentication request rejected."
                )

            raise RuntimeError(
                f"CDSE authentication failed.\n"
                f"Error: {error_code}\n"
                f"Description: {description}"
            )

        self.update_tokens(response.json())

        print(
            "CDSE authentication successful!",
            flush=True,
        )

    def refresh(self):

        if not self.refresh_token:
            self.login()
            return

        print(
            "Refreshing CDSE access token...",
            flush=True,
        )

        try:

            response = session.post(
                TOKEN_URL,
                data={
                    "client_id": "cdse-public",
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
                timeout=(15, 30),
            )

            if not response.ok:

                print(
                    "Refresh rejected. Logging in again.",
                    flush=True,
                )

                self.login()
                return

            self.update_tokens(response.json())

            print(
                "Access token refreshed.",
                flush=True,
            )

        except RequestException:

            self.login()

    def headers(self):

        if self.access_token is None:
            self.login()

        elif time.time() >= self.expires_at:
            self.refresh()

        return {
            "Authorization": (
                f"Bearer {self.access_token}"
            )
        }


auth = CDSEAuth()


# ============================================================
# 4. AUTHENTICATED GET REQUEST
# ============================================================

def authorized_get(url, stream=False):

    for attempt in range(1, RETRIES + 1):

        try:

            response = session.get(
                url,
                headers=auth.headers(),
                stream=stream,
                timeout=TIMEOUT,
            )

            if response.status_code == 401:

                response.close()

                auth.refresh()

                response = session.get(
                    url,
                    headers=auth.headers(),
                    stream=stream,
                    timeout=TIMEOUT,
                )

            if response.status_code in (
                429, 500, 502, 503, 504
            ):

                if attempt < RETRIES:

                    response.close()

                    wait = min(
                        15 * 2 ** (attempt - 1),
                        120,
                    )

                    print(
                        f"Server busy. Retrying in {wait}s...",
                        flush=True,
                    )

                    time.sleep(wait)
                    continue

            response.raise_for_status()

            return response

        except RequestException:

            if attempt == RETRIES:
                raise

            wait = min(
                15 * 2 ** (attempt - 1),
                120,
            )

            time.sleep(wait)


# ============================================================
# 5. LOAD STUDY AREA
# ============================================================

print("\nLoading province shapefile...")

aoi = gpd.read_file(SHAPEFILE)

if aoi.empty:
    raise ValueError("The shapefile contains no features.")

if aoi.crs is None:
    raise ValueError("The shapefile has no defined CRS.")

aoi = aoi.to_crs("EPSG:4326")

province = aoi.geometry.union_all()

search_geometry = box(*province.bounds)

print("Province:", Path(SHAPEFILE).stem)
print("Bounding box:", province.bounds)


# ============================================================
# 6. SEARCH SENTINEL-2 LEVEL-2A PRODUCTS
# ============================================================

def search_products():

    end_exclusive = (
        date.fromisoformat(END_DATE)
        + timedelta(days=1)
    ).isoformat()

    wkt = search_geometry.wkt

    filters = [
        "Collection/Name eq 'SENTINEL-2'",

        (
            "Attributes/OData.CSC.StringAttribute/any("
            "att:att/Name eq 'productType' and "
            "att/OData.CSC.StringAttribute/Value eq 'S2MSI2A')"
        ),

        (
            f"ContentDate/Start ge "
            f"{START_DATE}T00:00:00.000Z"
        ),

        (
            f"ContentDate/Start lt "
            f"{end_exclusive}T00:00:00.000Z"
        ),

        (
            "OData.CSC.Intersects("
            f"area=geography'SRID=4326;{wkt}')"
        ),
    ]

    if MAX_CLOUD_COVER is not None:
        filters.append(
            "Attributes/OData.CSC.DoubleAttribute/any("
            "att:att/Name eq 'cloudCover' and "
            "att/OData.CSC.DoubleAttribute/Value le "
            f"{MAX_CLOUD_COVER})"
        )

    params = {
        "$filter": " and ".join(filters),
        "$expand": "Attributes",
        "$orderby": "ContentDate/Start asc",
        "$top": 100,
    }

    url = f"{CATALOGUE_URL}/Products"

    products = []

    print("\nSearching all eligible Level-2A products...")

    while url:

        response = request_with_retry(
            "GET",
            url,
            params=params,
        )

        payload = response.json()
        response.close()

        products.extend(
            payload.get("value", [])
        )

        url = payload.get("@odata.nextLink")
        params = None

        print(
            f"Catalogue products found: {len(products)}",
            flush=True,
        )

    selected = []

    for product in products:

        footprint = product.get("GeoFootprint")

        if not footprint:
            continue

        try:
            if shape(footprint).intersects(province):
                selected.append(product)

        except (TypeError, ValueError):
            print(
                "Skipping invalid footprint:",
                product.get("Name", "Unknown"),
            )

    print(
        f"Products intersecting province: {len(selected)}"
    )

    return selected

# ============================================================
# SELECT THE TWO LEAST-CLOUDY ACQUISITIONS PER TILE
# ============================================================

from collections import defaultdict


def get_cloud_cover(product):

    for attribute in product.get("Attributes", []):

        if attribute.get("Name") == "cloudCover":

            value = attribute.get("Value")

            if value is not None:
                return float(value)

    return None


def get_tile_id(product):

    name = product.get("Name", "")

    match = re.search(
        r"_T(\d{2}[A-Z]{3})_",
        name,
    )

    if match:
        return "T" + match.group(1)

    return None


def get_acquisition_date(product):

    name = product.get("Name", "")

    match = re.search(
        r"_MSIL2A_(\d{8}T\d{6})_",
        name,
    )

    if not match:
        return None

    return match.group(1)[:8]


def select_best_products(products):

    grouped = defaultdict(list)

    missing_cloud = 0
    missing_tile = 0

    for product in products:

        tile_id = get_tile_id(product)
        cloud_cover = get_cloud_cover(product)

        if tile_id is None:
            missing_tile += 1
            continue

        if cloud_cover is None:
            missing_cloud += 1
            continue

        grouped[tile_id].append(product)

    selected = []

    print("\n" + "=" * 70)
    print("ADAPTIVE SENTINEL-2 CLOUD-COVER SELECTION")
    print("=" * 70)

    for tile_id in sorted(grouped):

        candidates = grouped[tile_id]

        # Lowest cloud cover first.
        # Product name provides a deterministic tie-break.
        candidates.sort(
            key=lambda p: (
                get_cloud_cover(p),
                p["Name"],
            )
        )

        tile_selected = []
        used_dates = set()

        for product in candidates:

            acquisition_date = get_acquisition_date(
                product
            )

            if acquisition_date is None:
                continue

            if (
                REQUIRE_DIFFERENT_DATES
                and acquisition_date in used_dates
            ):
                continue

            tile_selected.append(product)
            used_dates.add(acquisition_date)

            if len(tile_selected) >= IMAGES_PER_TILE:
                break

        selected.extend(tile_selected)

        print(
            f"\nTile: {tile_id} "
            f"({len(candidates)} candidates)"
        )

        for rank, product in enumerate(
            tile_selected,
            start=1,
        ):

            cloud = get_cloud_cover(product)
            acquisition = get_acquisition_date(product)

            print(
                f"  {rank}. Date: {acquisition} "
                f"| Cloud cover: {cloud:.2f}%"
            )

        if len(tile_selected) < IMAGES_PER_TILE:
            print(
                f"  WARNING: Only "
                f"{len(tile_selected)} distinct-date "
                f"acquisitions available."
            )

    print("\n" + "=" * 70)
    print("SELECTION SUMMARY")
    print("=" * 70)

    print("Unique MGRS tiles:", len(grouped))
    print("Catalogue candidates:", len(products))
    print("Products selected:", len(selected))
    print("Missing cloud metadata:", missing_cloud)
    print("Missing tile identifiers:", missing_tile)

    return selected

# ============================================================
# 7. CONSTRUCT SAFE NODE URL
# ============================================================

def node_url(product_id, parts=()):

    url = (
        f"{DOWNLOAD_URL}/Products({product_id})"
    )

    for part in parts:

        encoded = quote(
            part.replace("'", "''"),
            safe="",
        )

        url += f"/Nodes({encoded})"

    return url


# ============================================================
# 8. LIST FILES IN A SAFE DIRECTORY
# ============================================================

def list_nodes(product_id, parts=()):

    url = (
        node_url(product_id, parts)
        + "/Nodes"
    )

    nodes = []

    while url:

        with authorized_get(url) as response:
            payload = response.json()

        if isinstance(payload, list):

            nodes.extend(payload)
            break

        batch = payload.get(
            "result",
            payload.get("value", []),
        )

        if isinstance(batch, dict):
            batch = batch.get("value", [])

        nodes.extend(batch)

        url = payload.get("@odata.nextLink")

    return nodes


# ============================================================
# 9. DISCOVER ORIGINAL FILES
# ============================================================

def discover_files(product_id):

    found = []

    stack = [()]
    visited = set()

    while stack:

        parts = stack.pop()

        if parts in visited:
            continue

        visited.add(parts)

        print(
            "Checking:",
            "/".join(parts) or "PRODUCT ROOT",
            flush=True,
        )

        nodes = list_nodes(
            product_id,
            parts,
        )

        for node in nodes:

            name = node.get("Name", "")

            if not name:
                continue

            path = parts + (name,)

            children = int(
                node.get("ChildrenNumber") or 0
            )

            if children > 0:

                stack.append(path)

            else:

                if name.lower().endswith(
                    (".jp2", ".xml", ".gml")
                ):

                    found.append(path)

    return found


# ============================================================
# 10. SELECT BANDS, SCL AND METADATA
# ============================================================

def select_files(paths):

    selected = {
        band: []
        for band in BANDS
    }

    metadata = []

    for parts in paths:

        filename = parts[-1]

        for band, resolution in BANDS.items():

            pattern = (
                rf"_{band}_{resolution}\.jp2$"
            )

            if re.search(
                pattern,
                filename,
                re.IGNORECASE,
            ):

                selected[band].append(parts)

        if DOWNLOAD_METADATA:

            if filename.lower().endswith(
                (".xml", ".gml")
            ):

                metadata.append(parts)

    return selected, metadata


# ============================================================
# 11. DOWNLOAD ORIGINAL FILE
# ============================================================

def download_file(
    product_id,
    parts,
    destination,
):

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if SKIP_EXISTING:

        if (
            destination.exists()
            and destination.stat().st_size > 0
        ):

            print(
                "Skipping existing:",
                destination.name,
                flush=True,
            )

            return "skipped"

    url = (
        node_url(product_id, parts)
        + "/$value"
    )

    partial = destination.with_name(
        destination.name + ".part"
    )

    for attempt in range(1, RETRIES + 1):

        try:

            print(
                f"Downloading {destination.name} "
                f"({attempt}/{RETRIES})",
                flush=True,
            )

            with authorized_get(
                url,
                stream=True,
            ) as response:

                with open(
                    partial,
                    "wb",
                ) as file:

                    for chunk in (
                        response.iter_content(
                            chunk_size=1024 * 1024
                        )
                    ):

                        if chunk:
                            file.write(chunk)

            if (
                not partial.exists()
                or partial.stat().st_size == 0
            ):

                raise IOError(
                    "Downloaded file is empty."
                )

            os.replace(
                partial,
                destination,
            )

            print(
                "Saved:",
                destination.name,
                flush=True,
            )

            return "downloaded"

        except (
            RequestException,
            IOError,
            OSError,
        ) as exc:

            partial.unlink(
                missing_ok=True
            )

            if attempt == RETRIES:
                raise

            wait = min(
                15 * 2 ** (attempt - 1),
                120,
            )

            print(
                f"Download failed: {exc}\n"
                f"Retrying in {wait}s...",
                flush=True,
            )

            time.sleep(wait)


# ============================================================
# 12. MAIN DOWNLOAD PROCESS
# ============================================================

def main():

    print("\n" + "=" * 65)

    print(
        "SENTINEL-2 LEVEL-2A NATIVE DATA DOWNLOADER"
    )

    print("=" * 65)

    print(
        "Date range:",
        START_DATE,
        "to",
        END_DATE,
    )

    print(
        "Maximum cloud cover:",
        MAX_CLOUD_COVER,
        "%",
    )

    print(
        "Requested layers:",
        ", ".join(BANDS),
    )

    print(
        "Output:",
        OUTPUT_DIR,
    )

    # Search all eligible Sentinel-2 L2A products
    all_products = search_products()

    # Select the two least-cloudy acquisitions per MGRS tile
    products = select_best_products(all_products)

    # Save the selection before downloading
    selection_manifest = []

    for product in products:
        selection_manifest.append({
            "product": product["Name"],
            "id": product["Id"],
            "tile": get_tile_id(product),
            "acquisition_date": get_acquisition_date(product),
            "cloud_cover_percent": get_cloud_cover(product),
        })

    selection_file = (
            OUTPUT_DIR / "selected_products.json"
    )

    with open(
            selection_file,
            "w",
            encoding="utf-8",
    ) as f:

        json.dump(
            selection_manifest,
            f,
            indent=2,
        )

    print("\nSelection saved:", selection_file)

    if DRY_RUN:
        print("\nDRY RUN: No downloads performed.")
        return

    print(
        f"\nDownloading {len(products)} selected products..."
    )

    print(
        f"\nProducts selected: {len(products)}"
    )

    manifest = []

    for index, product in enumerate(
        products,
        start=1,
    ):

        product_id = product["Id"]
        product_name = product["Name"]

        print("\n" + "=" * 65)

        print(
            f"Product {index}/{len(products)}"
        )

        print(product_name)

        print("=" * 65)

        product_dir = (
            OUTPUT_DIR / product_name
        )

        product_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        try:

            paths = discover_files(
                product_id
            )

            selected, metadata = (
                select_files(paths)
            )

            missing = [
                band
                for band in BANDS
                if not selected[band]
            ]

            if missing:

                raise RuntimeError(
                    "Missing requested layers: "
                    + ", ".join(missing)
                )

            counts = {
                "downloaded": 0,
                "skipped": 0,
            }

            for band, resolution in (
                BANDS.items()
            ):

                print(
                    f"\nProcessing {band} "
                    f"({resolution})"
                )

                for parts in selected[band]:

                    destination = (
                        product_dir.joinpath(
                            *parts
                        )
                    )

                    result = download_file(
                        product_id,
                        parts,
                        destination,
                    )

                    counts[result] += 1

            if DOWNLOAD_METADATA:

                print(
                    "\nProcessing metadata..."
                )

                for parts in metadata:

                    destination = (
                        product_dir.joinpath(
                            *parts
                        )
                    )

                    result = download_file(
                        product_id,
                        parts,
                        destination,
                    )

                    counts[result] += 1

            catalogue_file = (
                product_dir
                / "catalogue_metadata.json"
            )

            with open(
                catalogue_file,
                "w",
                encoding="utf-8",
            ) as f:

                json.dump(
                    product,
                    f,
                    indent=2,
                )

            manifest.append({
                "product": product_name,
                "id": product_id,
                "status": "completed",
                "layers": {
                    band: len(selected[band])
                    for band in BANDS
                },
                "metadata_files": len(metadata),
                **counts,
            })

            print(
                "\nProduct completed successfully."
            )

        except Exception as exc:

            print(
                f"\nERROR processing "
                f"{product_name}: {exc}"
            )

            manifest.append({
                "product": product_name,
                "id": product_id,
                "status": "failed",
                "error": str(exc),
            })

        with open(
            OUTPUT_DIR
            / "download_manifest.json",
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                manifest,
                f,
                indent=2,
            )

    successful = sum(
        item["status"] == "completed"
        for item in manifest
    )

    failed = sum(
        item["status"] == "failed"
        for item in manifest
    )

    print("\n" + "=" * 65)

    print("DOWNLOAD PROCESS FINISHED")

    print("=" * 65)

    print(
        "Successful products:",
        successful,
    )

    print(
        "Failed products:",
        failed,
    )

    print(
        "Output directory:",
        OUTPUT_DIR,
    )

##
if __name__ == "__main__":
    main()
