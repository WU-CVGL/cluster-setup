"""The usage trend as SVG geometry, drawn server-side: download and upload stacked per bucket."""

WIDTH = 900
HEIGHT = 180
LEFT = 4
RIGHT = 8
TOP = 12
BOTTOM = 4


def bars(points, step, local):
    plot_w = WIDTH - LEFT - RIGHT
    plot_h = HEIGHT - TOP - BOTTOM
    n = max(1, len(points))
    peak = max([p["download"] + p["upload"] for p in points] + [0])
    scale = plot_h / peak if peak else 0
    slot = plot_w / n
    width = max(1.0, slot * 0.7)
    result = []
    for i, point in enumerate(points):
        down = point["download"] * scale
        up = point["upload"] * scale
        x = LEFT + i * slot + (slot - width) / 2
        base = TOP + plot_h
        result.append({
            "x": round(x, 1), "width": round(width, 1),
            "down_y": round(base - down, 1), "down_h": round(down, 1),
            "up_y": round(base - down - up, 1), "up_h": round(up, 1),
            "start": local(point["end"] - step), "end": local(point["end"]),
            "download": point["download"], "upload": point["upload"],
            "total": point["download"] + point["upload"],
        })
    return {"bars": result, "peak": peak, "width": WIDTH, "height": HEIGHT, "left": LEFT, "right": WIDTH - RIGHT,
            "top": TOP, "base": TOP + plot_h, "middle": TOP + plot_h / 2, "label_y": HEIGHT - 8,
            "first": local(points[0]["end"] - step) if points else None,
            "last": local(points[-1]["end"]) if points else None}
