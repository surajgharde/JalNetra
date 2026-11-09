import math
from sqlalchemy import select
from app.db.sync_session import sync_session
from app.db.models import WaterBody
from geoalchemy2.shape import to_shape
with sync_session() as s:
    wb = s.get(WaterBody, "wb_ambazari_lake")
    g = to_shape(wb.geom)
    minx, miny, maxx, maxy = g.bounds
    lon, lat = (minx+maxx)/2, (miny+maxy)/2
    for z in (14, 15, 16):
        n = 2 ** z
        x = int((lon + 180.0) / 360.0 * n)
        la = math.radians(lat)
        y = int((1.0 - math.asinh(math.tan(la)) / math.pi) / 2.0 * n)
        print(z, x, y)
    print("centre", round(lon,4), round(lat,4))
