@fused.udf(cache_max_age="7d")
def udf(
    tz: int = 12,
    tx: int = 660,
    ty: int = 1593,
    date: str = "2024-07-01",
    package: str = "",
    days: int = 30,
    what: str = "true_color",
    method: str = "best",
    cloud_cover: float = 80.0,
    cloud_mask: bool = True,
    cmap: str = "",
    vmin: float = None,
    vmax: float = None,
    min_zoom: int = 6,
):
    import base64
    import io

    import numpy as np
    from PIL import Image

    su = fused.load(package)
    try:
        rgba = np.asarray(
            su.module("su_reader").tile(
                tz, tx, ty, date, days=days, what=what, method=method, cloud_cover=cloud_cover,
                cloud_mask=cloud_mask, size=256, cmap=cmap or None, vmin=vmin, vmax=vmax, min_zoom=min_zoom,
            )
        )
    except Exception as e:
        if "NoDataError" not in type(e).__name__:
            raise
        rgba = np.zeros((4, 256, 256), "uint8")
    if not rgba[3].any():
        rgba = np.zeros((4, 256, 256), "uint8")
    buf = io.BytesIO()
    Image.fromarray(np.moveaxis(rgba, 0, -1), "RGBA").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()
