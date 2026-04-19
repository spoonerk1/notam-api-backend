import re
import math
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import geojson
from typing import Optional, Dict, Any, List, Union
from datetime import datetime
import json
import os
import motor.motor_asyncio
import certifi
from shapely.geometry import Polygon as ShapelyPolygon, LineString as ShapelyLineString, MultiPolygon as ShapelyMultiPolygon
from shapely.ops import split

app = FastAPI(title="NOTAM Visualizer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class NotamRequest(BaseModel):
    raw_text: str

# Base directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Setup MongoDB
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://spoonerk1_db:Gasanime%2BMon1526@cluster0.0ncpsnw.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
db = client.notam_db
notam_collection = db.notams
last_updated = datetime.now().isoformat()

def load_fir_boundaries():
    fir_file = os.path.join(BASE_DIR, 'fir_boundaries.json')
    if os.path.exists(fir_file):
        with open(fir_file, 'r') as f:
            return json.load(f)
    return {}

def parse_coordinate(coord_str: str) -> Optional[float]:
    """Parse a coordinate string like 263418N or 0502110E or 2531N into decimal degrees."""
    # N/S/E/W at the end
    match = re.search(r'^(\d+)([N|S|E|W])$', coord_str)
    if not match:
        return None
    
    digits, direction = match.groups()
    length = len(digits)
    
    if length == 4: # DDMM
        d = float(digits[0:2])
        m = float(digits[2:4])
        s = 0.0
    elif length == 5 and direction in ['E', 'W']: # DDDMM
        d = float(digits[0:3])
        m = float(digits[3:5])
        s = 0.0
    elif length == 6: # DDMMSS
        d = float(digits[0:2])
        m = float(digits[2:4])
        s = float(digits[4:6])
    elif length == 7 and direction in ['E', 'W']: # DDDMMSS
        d = float(digits[0:3])
        m = float(digits[3:5])
        s = float(digits[5:7])
    else:
        return None
        
    dec = d + (m / 60.0) + (s / 3600.0)
    
    if direction in ['S', 'W']:
        dec = -dec
        
    return dec

def nm_to_meters(nm: float) -> float:
    return nm * 1852.0

@app.get("/api/notams")
async def get_notams():
    """Return stored NOTAMs as a GeoJSON FeatureCollection."""
    features = []
    cursor = notam_collection.find({}, {"_id": 0}).sort([("_id", -1)])
    async for document in cursor:
        features.append(document)
        
    global last_updated
    return {
        "type": "FeatureCollection",
        "features": features,
        "last_updated": last_updated
    }

@app.post("/api/notams")
async def add_notam(request: NotamRequest):
    """Parse a new NOTAM and add it to the state."""
    global last_updated
    text = request.raw_text.upper().replace('\n', ' ')
    
    # 1. NOTAM ID (e.g., A0475/26)
    id_match = re.search(r'([A-Z]\d{4}/\d{2})', text)
    notam_id = id_match.group(1) if id_match else "UNKNOWN"
    
    # NOTAM Type (N, R, C)
    type_match = re.search(r'NOTAM([NRC])', text)
    notam_type = type_match.group(1) if type_match else "N"
    
    # FIR from Item A
    fir_match = re.search(r'A\)\s*([A-Z]{4})', text)
    fir = fir_match.group(1) if fir_match else "UNKNOWN"
    
    # 2. Time limits
    start_match = re.search(r'B\)\s*(\d{10})', text)
    end_match = re.search(r'C\)\s*(\d{10}(?:\s*EST)?|PERM)', text)
    start_time = start_match.group(1) if start_match else "UNKNOWN"
    end_time = end_match.group(1).strip() if end_match else "UNKNOWN"
    
    # 3. Item Q Parsing
    q_match = re.search(r'Q\)\s*(.+?)(?=\s*[A-Z]\)|$)', text)
    q_text = q_match.group(1) if q_match else ""
    
    # Altitude limits from Item Q (e.g., 000/999)
    alt_match = re.search(r'(\d{3}/\d{3})', q_text)
    altitude = alt_match.group(1) if alt_match else "UNKNOWN"
    
    # Circle Fallback from Item Q (e.g. 2531N05218E096)
    circle_match = re.search(r'(\d{4}[NS]\d{5}[EW])(\d{3})', q_text)
    point_coord = None
    radius_nm = 0
    if circle_match:
        coord_part = circle_match.group(1)
        radius_nm = float(circle_match.group(2))
        
        # split 2531N05218E into 2531N and 05218E
        lat_match = re.search(r'\d+[NS]', coord_part)
        lng_match = re.search(r'\d+[EW]', coord_part[lat_match.end():] if lat_match else coord_part)
        if lat_match and lng_match:
            lat_str = lat_match.group(0)
            lng_str = lng_match.group(0)
            lat = parse_coordinate(lat_str)
            lng = parse_coordinate(lng_str)
            if lat is not None and lng is not None:
                point_coord = (lng, lat)  # geojson is [lon, lat]

    # 4. Item E Parsing (Polygon coords)
    e_match = re.search(r'E\)\s*(.+?)(?=\s*[F-G]\)|$)', text)
    e_text = e_match.group(1).strip() if e_match else text
    
    # Find sequence of coordinates like 263418N0502110E or 263418N 0502110E or 263418N-0502110E
    polygon_coords = []
    
    # Look for coordinates: optionally split with space or dash
    coord_pairs = re.findall(r'(\d{4,6}[NS])[\s\-]*(\d{5,7}[EW])', e_text)
    for plat_str, plng_str in coord_pairs:
        plat = parse_coordinate(plat_str)
        plng = parse_coordinate(plng_str)
        if plat is not None and plng is not None:
            polygon_coords.append([plng, plat])
            
    # Also look for format where it's contiguous like 263418N0502110E without splitting in regex above
    if not polygon_coords:
        cont_matches = re.finditer(r'(\d{4,6}[NS])(\d{5,7}[EW])', e_text.replace(' ', '').replace('-', ''))
        for match in cont_matches:
            plat = parse_coordinate(match.group(1))
            plng = parse_coordinate(match.group(2))
            if plat is not None and plng is not None:
                polygon_coords.append([plng, plat])
                
    geometry = None
    split_features = []  # For dividing line cases that produce multiple features
    
    # Detect "open airspace" NOTAMs
    e_upper = e_text.upper()
    open_keywords = [
        "IS OPEN FOR OPERATIONS",
        "IS OPEN FOR OVERFLIGHT",
        "FIR IS OPEN",
        "AIRSPACE IS OPEN",
        "OPEN FOR ALL OPERATIONS",
        "AIRSPACE OPEN",
        "IS OPEN FOR ALL",
    ]
    is_open = any(kw in e_upper for kw in open_keywords)
    
    properties = {
        "id": notam_id,
        "notam_type": notam_type,
        "created_at": datetime.utcnow().isoformat() + "Z", # Store creation time to hide badge after 12h
        "fir": fir,
        "start": start_time,
        "end": end_time,
        "altitude": altitude,
        "item_e": e_text,
        "type": "none",
        "is_partial": "PARTIALLY" in e_upper or "WEST PART" in e_upper or "EAST PART" in e_upper or "PART REMAINS CLOSED" in e_upper,
        "is_open": is_open
    }
    
    # Load FIR dictionaries dynamically to reflect updates
    fir_boundaries_dict = load_fir_boundaries()
    
    # Priority 1.5: Dividing Line Waypoints (e.g. partial FIR closure with a named dividing line)
    # These waypoints define a line that splits an FIR into open/closed parts.
    # Ordered dict: the sequence matters for drawing the line correctly.
    dividing_line_db = {
        # Tehran FIR partial closure dividing line (NOTAM A0751/26 pattern)
        "IVIVA": [57.8272222, 25.0025000],  # 250009N0574938E  [lng, lat]
        "PURBO": [56.9725000, 31.2347222],  # 311405N0565821E
        "ANK":   [53.7275000, 33.5408333],  # 333227N0534339E
        "OBRIX": [52.1422222, 34.7633333],  # 344548N0520832E
        "NAGMO": [51.3463889, 36.0383333],  # 360218N0512047E
        "LALDA": [49.7500000, 38.2722222],  # 381620N0494500E (also in primary_waypoints_db)
    }
    
    # Priority 2: Regular Waypoints found in text
    primary_waypoints_db = {
        "ULDUS": [51.0155556, 38.0022222],  # 380008N0510056E
        "BATEV": [50.2361111, 38.1719444],  # 381019N0501410E
        "LALDA": [49.7500000, 38.2722222],  # 381620N0494500E
        "PARSU": [49.3025000, 39.4786111]   # 392843N0491809E
    }
    
    alt_waypoints_db = {
        "MATAL": [45.5008333, 40.7708333],  # 404615N0453003E
        "MARAL": [51.4980556, 41.3627778],  # 412146N0512953E
        "METKA": [51.4983333, 40.7261111],  # 404334N0512954E
        "RODAR": [51.4969444, 40.4683333],  # 402806N0512949E
        "LARGI": [51.4975000, 40.2725000]   # 401621N0512951E
    }
    
    # Check for dividing line pattern first (only if no polygon coords found)
    found_dividing = []  # list of {name, coord} in order they appear in text
    if not (len(polygon_coords) >= 3):
        for wp_name, coord in dividing_line_db.items():
            if wp_name in text:
                found_dividing.append({"name": wp_name, "coords": coord})
    
    found_primary = []
    found_alt = []
    waypoint_details = []
    
    # Only search regular Waypoints if there's no Polygon AND no dividing line
    if not (len(polygon_coords) >= 3) and len(found_dividing) < 2:
        for wp_name, coord in primary_waypoints_db.items():
            if wp_name in text:
                found_primary.append(coord)
                waypoint_details.append({"name": wp_name, "type": "primary", "coords": coord})
        for wp_name, coord in alt_waypoints_db.items():
            if wp_name in text:
                found_alt.append(coord)
                waypoint_details.append({"name": wp_name, "type": "alt", "coords": coord})

    if len(polygon_coords) >= 3:
        # close the polygon if not closed
        if polygon_coords[0] != polygon_coords[-1]:
            polygon_coords.append(polygon_coords[0])
        geometry = geojson.Polygon([polygon_coords])
        properties["type"] = "polygon"

    elif len(found_dividing) >= 2:
        # Dividing line: split the FIR polygon into closed/open halves
        line_coords = [wp["coords"] for wp in found_dividing]  # [lng, lat]
        
        # Determine which FIR to split based on Item A code
        fir_boundaries_dict_local = load_fir_boundaries()
        icao_to_region_local = {
            "OBBB": "BAHRAIN", "OIIX": "TEHRAN", "LTAA": "ANKARA", "LTBB": "ISTANBUL",
            "ORBB": "BAGHDAD", "LLLL": "TEL-AVIV", "LLAD": "TEL-AVIV", "OJAC": "AMMAN",
            "OKAC": "KUWAIT", "OLBA": "BEIRUT", "OOMM": "MUSCAT", "OTDF": "DOHA",
            "OEJD": "JEDDAH", "OSTT": "DAMASCUS", "OMAE": "EMIRATES", "OYSN": "SANAA",
            "UBBA": "BAKU"
        }
        fir_region = icao_to_region_local.get(fir, None)
        fir_poly_data = fir_boundaries_dict_local.get(fir_region) if fir_region else None
        
        split_features = []
        
        if fir_poly_data:
            try:
                # Build shapely polygon from FIR boundary
                if fir_poly_data['type'] == 'MultiPolygon':
                    fir_shapely = ShapelyMultiPolygon([ShapelyPolygon(ring) for ring in [coords[0] for coords in fir_poly_data['coordinates']]])
                else:
                    fir_shapely = ShapelyPolygon(fir_poly_data['coordinates'][0])
                
                # Extend the dividing line beyond FIR boundary so it cleanly cuts through
                p_start = line_coords[0]
                p_end = line_coords[-1]
                # Extend start southward and end northward by ~5 degrees
                dx_s = line_coords[1][0] - p_start[0]
                dy_s = line_coords[1][1] - p_start[1]
                ext_start = [p_start[0] - dx_s * 3, p_start[1] - dy_s * 3]
                dx_e = p_end[0] - line_coords[-2][0]
                dy_e = p_end[1] - line_coords[-2][1]
                ext_end = [p_end[0] + dx_e * 3, p_end[1] + dy_e * 3]
                
                extended_line_coords = [ext_start] + line_coords + [ext_end]
                cutting_line = ShapelyLineString(extended_line_coords)
                
                result_geoms = split(fir_shapely, cutting_line)
                
                if len(result_geoms.geoms) >= 2:
                    # Determine west vs east by comparing centroid longitude
                    parts = list(result_geoms.geoms)
                    parts.sort(key=lambda p: p.centroid.x)  # lowest lng = west
                    
                    # Detect which side is closed from text
                    west_closed = "WEST" in text and ("CLOSED" in text or "REMAINS CLOSED" in text)
                    east_closed = "EAST" in text and ("CLOSED" in text or "REMAINS CLOSED" in text) and "WEST" not in text
                    
                    # More nuanced: if text says "EAST PART...RESUMED NORMAL" and "WEST PART REMAINS CLOSED"
                    if "WEST PART REMAINS CLOSED" in text or "WEST PART" in text and "CLOSED" in text:
                        west_closed = True
                    if "EAST PART" in text and ("RESUMED" in text or "OPEN" in text or "NORMAL OPERATION" in text):
                        east_closed = False
                    
                    for i, part_poly in enumerate(parts):
                        is_west = (i == 0)  # sorted by lng, first = west
                        
                        if is_west:
                            side_closed = west_closed
                            side_label = f"{fir_region} FIR (WEST - {'CLOSED' if side_closed else 'OPEN'})"
                        else:
                            side_closed = east_closed if east_closed else False
                            side_label = f"{fir_region} FIR (EAST - {'CLOSED' if side_closed else 'OPEN'})"
                        
                        # Convert shapely polygon back to GeoJSON coords
                        ext_ring = list(part_poly.exterior.coords)
                        part_geojson = geojson.Polygon([ext_ring])
                        
                        part_props = dict(properties)  # copy base properties
                        part_props["type"] = "polygon"
                        # Closed side = RED (is_partial=False, is_open=False)
                        # Open side = GREEN (is_open=True)
                        part_props["is_partial"] = False
                        part_props["is_open"] = not side_closed
                        part_props["item_e"] = side_label + "\n" + e_text
                        part_props["fir_side"] = "west" if is_west else "east"
                        
                        split_features.append(geojson.Feature(geometry=part_geojson, properties=part_props))
                    
                    # Also add the dividing line itself as a visual overlay
                    line_geojson = geojson.LineString(line_coords)
                    line_props = dict(properties)
                    line_props["type"] = "dividing_line"
                    line_props["dividing_waypoints"] = found_dividing
                    split_features.append(geojson.Feature(geometry=line_geojson, properties=line_props))
                    
            except Exception as e:
                print(f"Warning: FIR split failed: {e}. Falling back to LineString.")
        
        if not split_features:
            # Fallback: just draw the dividing line if polygon split failed
            geometry = geojson.LineString(line_coords)
            properties["type"] = "dividing_line"
            properties["dividing_waypoints"] = found_dividing
        
    elif len(found_primary) > 0 or len(found_alt) > 0:
        all_found = found_primary + found_alt
        if len(all_found) == 1:
            geometry = geojson.Point(all_found[0])
        else:
            geometry = geojson.MultiPoint(all_found)
            
        properties["waypoint_list"] = waypoint_details
        
        # Distinguish type for frontend coloring
        if len(found_primary) > 0 and len(found_alt) == 0:
            properties["type"] = "waypoint"
        elif len(found_alt) > 0 and len(found_primary) == 0:
            properties["type"] = "waypoint_alt"
        else:
            properties["type"] = "waypoint_both"
        
    # Priority 3: FIR Fallback based on external JSON definitions
    elif any(f"{fir_key} FIR" in text for fir_key in fir_boundaries_dict.keys()) or ("OTDF FIR" in text and "DOHA" in fir_boundaries_dict) or fir in ["UBBA"] or "BAKU FIR" in text or fir != "UNKNOWN":
        found_key = None

        # ICAO to Region name fallback mapping
        icao_to_region = {
            "OBBB": "BAHRAIN", "OIIX": "TEHRAN", "LTAA": "ANKARA", "LTBB": "ISTANBUL",
            "ORBB": "BAGHDAD", "LLLL": "TEL-AVIV", "LLAD": "TEL-AVIV", "OJAC": "AMMAN",
            "OKAC": "KUWAIT", "OLBA": "BEIRUT", "OOMM": "MUSCAT", "OTDF": "DOHA",
            "OEJD": "JEDDAH", "OSTT": "DAMASCUS", "OMAE": "EMIRATES", "OYSN": "SANAA",
            "UBBA": "BAKU"
        }

        # --- Priority A: Item A ICAO code (most authoritative) ---
        if fir in icao_to_region:
            mapped_region = icao_to_region[fir]
            if mapped_region in fir_boundaries_dict:
                found_key = mapped_region

        # --- Priority B: OTDF alias ---
        if not found_key and "OTDF FIR" in text:
            found_key = "DOHA"

        # --- Priority C: Free-text scan (last resort, avoids false positives) ---
        # Only run if Item A lookup failed, and restrict to matching the *subject* FIR
        # by looking for the FIR name in the first ~300 characters of Item E.
        if not found_key:
            e_head = e_text[:300]
            found_key = next((k for k in fir_boundaries_dict.keys() if f"{k} FIR" in e_head), None)

        # If the subject-heading scan still fails, widen to full text as last resort
        if not found_key:
            found_key = next((k for k in fir_boundaries_dict.keys() if f"{k} FIR" in text), None)

        # --- Special logic for Baku Sectors ---
        if fir == "UBBA" or "BAKU FIR" in text:
            if "SECTOR SOUTH" in text and "BAKU_SOUTH" in fir_boundaries_dict:
                found_key = "BAKU_SOUTH"
            elif "BAKU" in fir_boundaries_dict:  # Fallback to whole Baku FIR if it existed
                found_key = "BAKU"
                
        if found_key and found_key in fir_boundaries_dict:
            fir_data = fir_boundaries_dict[found_key]
            if fir_data['type'] == 'MultiPolygon':
                geometry = geojson.MultiPolygon(fir_data['coordinates'])
            else:
                geometry = geojson.Polygon(fir_data['coordinates'])
            properties["type"] = "polygon"
            properties["item_e"] += f" ({found_key} Boundary Extrapolated)"
        # Priority 4: Point with radius
        elif point_coord is not None and radius_nm > 0:
            geometry = geojson.Point(point_coord)
            properties["radius_meters"] = nm_to_meters(radius_nm)
            properties["type"] = "circle"
        else:
            raise HTTPException(status_code=400, detail="Could not extract Polygon, Circle, or Waypoint coordinates from NOTAM.")
    # Priority 4 fallback if FIR logic ran but didn't find key
    elif point_coord is not None and radius_nm > 0:
        geometry = geojson.Point(point_coord)
        properties["radius_meters"] = nm_to_meters(radius_nm)
        properties["type"] = "circle"
    else:
        raise HTTPException(status_code=400, detail="Could not extract Polygon, Circle, or Waypoint coordinates from NOTAM.")
        
    # Handle multi-feature case (dividing line split into west/east polygons + line)
    if split_features:
        # Delete any existing features for this NOTAM
        await notam_collection.delete_many({"properties.id": notam_id})
        
        # Insert each split feature separately with a sub-id
        for idx, sf in enumerate(split_features):
            sf_dict = dict(sf)
            sf_dict["properties"]["id"] = notam_id  # keep same ID for grouping
            sf_dict["properties"]["_split_index"] = idx
            await notam_collection.insert_one(sf_dict)
        
        last_updated = datetime.now().isoformat()
        return {"status": "success", "message": f"Added NOTAM ID {notam_id} (split into {len(split_features)} features)"}
    
    feature = geojson.Feature(geometry=geometry, properties=properties)
    
    # Drop _id before inserting to avoid type issues with geojson if needed, but not required
    # Create the dict representation of the feature
    feature_dict = dict(feature)
    
    # Check if this NOTAM already exists and replace, or insert new
    await notam_collection.replace_one({"properties.id": notam_id}, feature_dict, upsert=True)
    
    last_updated = datetime.now().isoformat()
    
    return {"status": "success", "message": f"Added NOTAM ID {notam_id}"}

@app.delete("/api/notams")
async def clear_notams():
    """Clear all stored NOTAMs."""
    global last_updated
    await notam_collection.delete_many({})
    last_updated = datetime.now().isoformat()
    return {"status": "success", "message": "All NOTAMs cleared."}

@app.delete("/api/notams/{notam_id:path}")
async def delete_notam(notam_id: str):
    """Delete a specific NOTAM by ID."""
    global last_updated
    
    # Remove from MongoDB
    result = await notam_collection.delete_one({"properties.id": notam_id})
    
    if result.deleted_count > 0:
        last_updated = datetime.now().isoformat()
        return {"status": "success", "message": f"NOTAM {notam_id} deleted."}
    else:
        raise HTTPException(status_code=404, detail=f"NOTAM ID {notam_id} not found.")
