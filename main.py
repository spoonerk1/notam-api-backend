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
    cursor = notam_collection.find({}, {"_id": 0})
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
    properties = {
        "id": notam_id,
        "start": start_time,
        "end": end_time,
        "altitude": altitude,
        "item_e": e_text,
        "type": "none"
    }
    
    # Load FIR dictionaries dynamically to reflect updates
    fir_boundaries_dict = load_fir_boundaries()
    
    # Priority 1: Polygon
    if len(polygon_coords) >= 3:
        # close the polygon if not closed
        if polygon_coords[0] != polygon_coords[-1]:
            polygon_coords.append(polygon_coords[0])
        geometry = geojson.Polygon([polygon_coords])
        properties["type"] = "polygon"
        
    # Priority 2: FIR Fallback based on external JSON definitions
    elif any(f"{fir_key} FIR" in text for fir_key in fir_boundaries_dict.keys()) or ("OTDF FIR" in text and "DOHA" in fir_boundaries_dict):
        found_key = next((k for k in fir_boundaries_dict.keys() if f"{k} FIR" in text), None)
        if not found_key and "OTDF FIR" in text:
            found_key = "DOHA"
            
        if found_key:
            fir_data = fir_boundaries_dict[found_key]
            if fir_data['type'] == 'MultiPolygon':
                geometry = geojson.MultiPolygon(fir_data['coordinates'])
            else:
                geometry = geojson.Polygon(fir_data['coordinates'])
            properties["type"] = "polygon"
            properties["item_e"] += f" ({found_key} FIR Boundary Extrapolated)"
    # Priority 3: Point with radius
    elif point_coord is not None and radius_nm > 0:
        geometry = geojson.Point(point_coord)
        properties["radius_meters"] = nm_to_meters(radius_nm)
        properties["type"] = "circle"
    else:
        raise HTTPException(status_code=400, detail="Could not extract Polygon or Circle coordinates from NOTAM.")
        
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

@app.delete("/api/notams/{notam_id}")
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
