# PyPI cache (proxpi)

The `proxpi` service in [`docker-compose.yml`](../docker-compose.yml) runs [proxpi](https://github.com/EpicWink/proxpi), a caching proxy of the PyPI simple index, for the nodes and the Determined tasks. What it serves and how clients use it: [PyPI cache](../../docs/04_Setup_Supplementary_Services.md#pypi-cache).

- Index: the BFSU mirror (`https://mirrors.bfsu.edu.cn/pypi/web/simple/`), with `https://pypi.org/simple/` as an extra index for files the mirror lacks.
- URL: `http://192.168.233.8:5000/index/` (cluster network), `http://10.0.1.68:5000/index/` (campus).
- Files: `data/` (gitignored, on the VM's local disk), capped by `PROXPI_CACHE_SIZE` (bytes, an integer only); the least-used files go first. They survive restarts; the index listings are kept in memory only.
- A file not cached yet is answered with a redirect to its upstream (the mirror, or PyPI for fallback files) while proxpi downloads it in the background, so clients must reach both themselves (the nodes and tasks do, without a proxy).

## Operations

```bash
docker compose up -d proxpi                    # in services/
docker compose ps proxpi                       # (healthy): GET /health answers
docker compose logs --tail 50 proxpi
curl http://192.168.233.8:5000/index/numpy/    # a project's listing
```

A new release on the mirror shows up within `PROXPI_INDEX_TTL` (30 minutes) or, from PyPI, `PROXPI_EXTRA_INDEX_TTLS` (30 minutes). To refresh at once:

```bash
curl -X DELETE http://192.168.233.8:5000/cache/<project>   # one project's listing
curl -X DELETE http://192.168.233.8:5000/cache/list        # the list of projects
```

To empty the file cache, stop the service, delete the contents of `data/` and start it again.

An error from the extra index other than "not found" (PyPI unreachable or timing out) fails the listing of a project with HTTP 500 until it answers again; `PROXPI_CONNECT_TIMEOUT` and `PROXPI_READ_TIMEOUT` bound the wait. A client can fall back to the mirror directly: `pip install --index-url https://mirrors.bfsu.edu.cn/pypi/web/simple ...`.
