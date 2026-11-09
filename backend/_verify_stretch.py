"""Before/after: how much of the colour ramp each layer actually uses, and how
many distinct colours a rendered tile contains."""
import asyncio
import io as _io

import httpx
from PIL import Image

from app.api.tiles import LAYER_STYLES, adaptive_rescale, layer_of, titiler_params
from app.core.config import get_settings

S = get_settings()
CHIPS = [
    "chips/wb_ambazari_lake/2026-10-19/body/sediment_proxy.tif",
    "chips/wb_ambazari_lake/2026-10-19/body/ndci_chlorophyll.tif",
    "chips/wb_ambazari_lake/2026-10-19/body/ndti_turbidity.tif",
    "chips/wb_ambazari_lake/2026-10-19/body/fai_algal.tif",
    "chips/wb_ambazari_lake/2026-10-19/body/mndwi_extent.tif",
]
# A tile covering Ambazari Lake (z15).
Z, X, Y = 15, 23577, 14415


async def render(chip, rescale):
    url = f"{S.titiler_url.rstrip('/')}/cog/tiles/WebMercatorQuad/{Z}/{X}/{Y}.png"
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.get(url, params=titiler_params(chip, S, rescale=rescale))
    if r.status_code != 200:
        return None
    im = Image.open(_io.BytesIO(r.content)).convert("RGBA")
    px = [p for p in im.getdata() if p[3] > 0]  # opaque = inside the chip
    return len(set(px)), len(px)


async def main():
    print(f"{'layer':<18} {'fixed ramp':<16} {'adaptive ramp':<22} "
          f"{'colours fixed':>13} {'colours adaptive':>17}")
    for chip in CHIPS:
        layer = layer_of(chip)
        fixed = LAYER_STYLES[layer].rescale
        adaptive = await adaptive_rescale(chip, S)
        a = await render(chip, None)
        b = await render(chip, adaptive)
        fa = f"{a[0]}/{a[1]}" if a else "n/a"
        fb = f"{b[0]}/{b[1]}" if b else "n/a"
        adv = (
            f"({adaptive[0]:.3f}, {adaptive[1]:.3f})" if adaptive else "-- kept fixed --"
        )
        print(f"{layer:<18} {str(fixed):<16} {adv:<22} {fa:>13} {fb:>17}")


asyncio.run(main())
