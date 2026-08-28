"""Ücretsiz Sentinel-2 görüntülerini bulur ve kaba arazi değişimini karşılaştırır."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO

import numpy as np
import requests
from PIL import Image


EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
MIN_HOTSPOT_AREA_M2 = 250

# Batı, güney, doğu, kuzey (WGS84)
REGIONS = {
    "cesme": {"label": "Çeşme merkez · Alaçatı · Ilıca", "bbox": [26.27, 38.235, 26.47, 38.365]},
    "uzunkuyu": {"label": "Uzunkuyu · Germiyan · Ildır", "bbox": [26.45, 38.225, 26.64, 38.385]},
    "all": {"label": "Tüm Çeşme + Uzunkuyu", "bbox": [26.25, 38.22, 26.64, 38.43]},
}

PLACE_CENTERS = {
    "Çeşme": (38.3226, 26.3067), "Alaçatı": (38.2848, 26.3745),
    "Ilıca": (38.3084, 26.3607), "Reisdere": (38.3158, 26.4173),
    "Ovacık": (38.2587, 26.3370), "Dalyan": (38.3540, 26.3070),
    "Çiftlikköy": (38.2715, 26.2660), "Musalla": (38.3170, 26.3020),
    "Şifne": (38.3300, 26.4280), "Germiyan": (38.3220, 26.4970),
    "Uzunkuyu": (38.2843, 26.5510), "Ildır": (38.3840, 26.4840),
}

class SatelliteError(RuntimeError):
    """Uydu arama veya görüntü okuma hatası."""


def _search_items(bbox, days=65, max_cloud=25):
    end = datetime.now(timezone.utc); start = end - timedelta(days=days)
    payload = {"collections": ["sentinel-2-c1-l2a"], "bbox": bbox,
        "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
        "query": {"eo:cloud_cover": {"lt": max_cloud}}, "limit": 40,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}]}
    response = requests.post(EARTH_SEARCH_URL, json=payload, timeout=45); response.raise_for_status()
    features = response.json().get("features", [])
    usable = [f for f in features if all(k in f.get("assets", {}) for k in ("visual", "red", "nir", "scl"))]
    if len(usable) < 2: raise SatelliteError("Son dönemde karşılaştırılabilir iki görüntü bulunamadı.")
    return usable


def _pick_pair(items, minimum_gap_days=2):
    latest = items[0]; latest_time = datetime.fromisoformat(latest["properties"]["datetime"].replace("Z", "+00:00"))
    for older in items[1:]:
        older_time = datetime.fromisoformat(older["properties"]["datetime"].replace("Z", "+00:00"))
        if (latest_time - older_time).total_seconds() >= minimum_gap_days * 86400: return older, latest
    return items[1], latest


def sentinel_pair(region_key):
    if region_key not in REGIONS: raise SatelliteError("Bilinmeyen uydu bölgesi.")
    return _pick_pair(_search_items(REGIONS[region_key]["bbox"]))


def _output_shape(bbox, max_width=1050):
    west, south, east, north = bbox; mean_lat = (south + north) / 2
    width_km = (east-west)*111.32*np.cos(np.radians(mean_lat)); height_km=(north-south)*110.57
    return min(max(320, round(max_width*height_km/max(width_km,0.1))),850), max_width


def _read_asset(item, asset_name, bbox, height, width, resampling_name):
    try:
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.windows import from_bounds
        from rasterio.warp import transform_bounds
    except ImportError as exc: raise SatelliteError("Uydu görüntü işleme bağımlılıkları kurulamadı.") from exc
    href=item["assets"][asset_name]["href"]; resampling=getattr(Resampling,resampling_name)
    env={"GDAL_DISABLE_READDIR_ON_OPEN":"EMPTY_DIR","CPL_VSIL_CURL_ALLOWED_EXTENSIONS":".tif","GDAL_HTTP_MAX_RETRY":"3","GDAL_HTTP_RETRY_DELAY":"1"}
    with rasterio.Env(**env):
        with rasterio.open(href) as source:
            projected=transform_bounds("EPSG:4326",source.crs,*bbox,densify_pts=21); window=from_bounds(*projected,transform=source.transform)
            return source.read(out_shape=(source.count,height,width),window=window,resampling=resampling,boundless=True,fill_value=0)


def _reflectance(raw): return np.clip(raw.astype("float32")*0.0001-0.1,0,1)
def _ndvi(red,nir):
    d=nir+red; return np.divide(nir-red,d,out=np.zeros_like(red,dtype="float32"),where=d>0.001)

def _clean_mask(mask):
    padded=np.pad(mask.astype("uint8"),1,mode="constant"); votes=np.zeros(mask.shape,dtype="uint8")
    for r in range(3):
        for c in range(3): votes += padded[r:r+mask.shape[0],c:c+mask.shape[1]]
    return mask & (votes>=4)

def _png_bytes(array):
    output=BytesIO(); Image.fromarray(array).save(output,format="PNG",optimize=True); return output.getvalue()

def _item_date(item): return datetime.fromisoformat(item["properties"]["datetime"].replace("Z","+00:00")).strftime("%d.%m.%Y")
def _nearest_place(latitude,longitude):
    cosine=np.cos(np.radians(latitude)); return min(PLACE_CENTERS,key=lambda n:(PLACE_CENTERS[n][0]-latitude)**2+((PLACE_CENTERS[n][1]-longitude)*cosine)**2)


def _connected_components(mask):
    rows,columns=np.nonzero(mask)
    if not len(rows): return []
    height,width=mask.shape; visited=np.zeros(mask.shape,dtype=bool); neighbors=((-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)); components=[]
    for sr,sc in zip(rows.tolist(),columns.tolist()):
        if visited[sr,sc]: continue
        visited[sr,sc]=True; stack=[(sr,sc)]; component=[]
        while stack:
            row,column=stack.pop(); component.append((row,column))
            for ro,co in neighbors:
                nr,nc=row+ro,column+co
                if 0<=nr<height and 0<=nc<width and mask[nr,nc] and not visited[nr,nc]: visited[nr,nc]=True; stack.append((nr,nc))
        components.append(component)
    return components


def _hotspots(change_mask,bbox,pixel_area_m2,limit=10):
    components=_connected_components(change_mask)
    if not components: return []
    height,width=change_mask.shape; west,south,east,north=bbox; results=[]
    for component in components:
        area_m2=len(component)*pixel_area_m2
        if area_m2 < MIN_HOTSPOT_AREA_M2: continue
        pixels=np.asarray(component,dtype="int32"); centroid=pixels.mean(axis=0); representative=pixels[int(np.argmin(np.sum((pixels-centroid)**2,axis=1)))]; row,column=map(int,representative)
        latitude=north-(row+0.5)/height*(north-south); longitude=west+(column+0.5)/width*(east-west)
        results.append({"mahalle":_nearest_place(latitude,longitude),"enlem":round(latitude,6),"boylam":round(longitude,6),"alan_m2":round(area_m2),"sinyal":"Bitişik yüzey/toprak değişimi adayı"})
    return sorted(results,key=lambda item:item["alan_m2"],reverse=True)[:limit]


def analyze_sentinel_change(region_key,pair=None):
    if region_key not in REGIONS: raise SatelliteError("Bilinmeyen uydu bölgesi.")
    region=REGIONS[region_key]; bbox=region["bbox"]; older,latest=pair or sentinel_pair(region_key); height,width=_output_shape(bbox)
    older_visual=_read_asset(older,"visual",bbox,height,width,"bilinear")[:3]; latest_visual=_read_asset(latest,"visual",bbox,height,width,"bilinear")[:3]
    older_red=_reflectance(_read_asset(older,"red",bbox,height,width,"bilinear")[0]); latest_red=_reflectance(_read_asset(latest,"red",bbox,height,width,"bilinear")[0])
    older_nir=_reflectance(_read_asset(older,"nir",bbox,height,width,"bilinear")[0]); latest_nir=_reflectance(_read_asset(latest,"nir",bbox,height,width,"bilinear")[0])
    older_scl=_read_asset(older,"scl",bbox,height,width,"nearest")[0]; latest_scl=_read_asset(latest,"scl",bbox,height,width,"nearest")[0]
    excluded=np.array([0,1,3,6,8,9,10,11]); valid=~np.isin(older_scl,excluded); valid &= ~np.isin(latest_scl,excluded)
    old_rgb=np.moveaxis(older_visual,0,2).astype("float32")/255; new_rgb=np.moveaxis(latest_visual,0,2).astype("float32")/255
    rgb_difference=np.mean(np.abs(new_rgb-old_rgb),axis=2); brightness_gain=np.mean(new_rgb,axis=2)-np.mean(old_rgb,axis=2); vegetation_loss=_ndvi(older_red,older_nir)-_ndvi(latest_red,latest_nir)
    soil_signal=valid & (vegetation_loss>0.14) & (brightness_gain>0.035) & (rgb_difference>0.10); strong_visual_change=valid & (rgb_difference>0.24); change_mask=_clean_mask(soil_signal|strong_visual_change)
    latest_rgb=np.moveaxis(latest_visual,0,2).astype("uint8"); overlay=latest_rgb.copy(); overlay[change_mask]=(overlay[change_mask].astype("float32")*0.30+np.array([255,55,25],dtype="float32")*0.70).astype("uint8")
    west,south,east,north=bbox; pixel_width_m=(east-west)*111320*np.cos(np.radians((south+north)/2))/width; pixel_height_m=(north-south)*110570/height; pixel_area_m2=pixel_width_m*pixel_height_m
    changed_km2=float(change_mask.sum()*pixel_area_m2/1e6); valid_pixels=max(int(valid.sum()),1)
    return {"region":region["label"],"older_date":_item_date(older),"latest_date":_item_date(latest),"older_cloud":float(older["properties"].get("eo:cloud_cover",0)),"latest_cloud":float(latest["properties"].get("eo:cloud_cover",0)),"latest_png":_png_bytes(latest_rgb),"change_png":_png_bytes(overlay),"changed_km2":changed_km2,"changed_percent":float(change_mask.sum()/valid_pixels*100),"hotspots":_hotspots(change_mask,bbox,pixel_area_m2),"latest_item":latest["id"],"older_item":older["id"]}
