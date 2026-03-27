"""
OSM 瓦片代理 — 解决 Docker 容器内 Clash fake-ip DNS 劫持问题

原理: agent-service 容器通过 HTTPS_PROXY 环境变量走宿主机 Clash 代理访问 OSM，
      nginx 将 /osm-tiles/ 请求转发到此接口。
"""
import os
from fastapi import APIRouter, Response
from fastapi.responses import Response as FastAPIResponse
from loguru import logger

router = APIRouter(prefix="/api/v1/tiles", tags=["tiles"])

# 瓦片源配置（从环境变量或默认值）
_TILE_SOURCES = {
    "osm": os.environ.get("OSM_TILE_URL", "https://tile.openstreetmap.org"),
    "hot": os.environ.get("OSM_HOT_URL", "https://a.tile.openstreetmap.fr/hot"),
    "topo": os.environ.get("OSM_TOPO_URL", "https://a.tile.opentopomap.org"),
    "cycle": os.environ.get("OSM_CYCLE_URL", "https://a.tile.cyclemap.osm.cz/cycle"),
}

# 缓存头: 浏览器缓存 7 天，nginx 层面也可以 proxy_cache
_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=604800",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/{source}/{z}/{x}/{y}.png")
async def proxy_tile(source: str, z: int, x: int, y: int):
    """
    代理瓦片请求

    GET /api/v1/tiles/osm/14/13725/7013.png
    → https://tile.openstreetmap.org/14/13725/7013.png
    """
    base_url = _TILE_SOURCES.get(source)
    if not base_url:
        return Response(content=b"Unknown tile source", status_code=404)

    tile_url = f"{base_url}/{z}/{x}/{y}.png"

    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
        ) as client:
            resp = await client.get(
                tile_url,
                headers={
                    "User-Agent": "STHG-Agent-TileProxy/1.0",
                    "Accept": "image/png,image/*",
                },
            )

            if resp.status_code != 200:
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                )

            return Response(
                content=resp.content,
                media_type="image/png",
                headers=_CACHE_HEADERS,
            )

    except Exception as e:
        logger.warning(f"[TileProxy] Failed to fetch {tile_url}: {e}")
        return Response(content=b"Tile fetch failed", status_code=502)
