I'll check a few read-only facts first (how that Superset runs and which drivers it already has), so the steps fit your setup.

You can add both through Superset's web UI. Trino just needs a connection string; Oracle needs a driver installed first, which means editing two files and restarting.

Two things are different from your JDBC strings:
- **Connection strings:** Superset uses SQLAlchemy URIs, not `jdbc:` URLs.
- **`localhost`:** Superset runs in Docker, where `localhost` means the Superset container itself. Use `host.docker.internal` instead. Your compose file already maps it to the Mac, and Oracle (1521) and Trino (8090) are published there.

## Trino

The driver (`trino` 0.330.0) is already installed in the container. Use this URI:

```
trino://trino@host.docker.internal:8090/iceberg
```

Add `/default` on the end if you want that schema preselected. Leaving it off lets you pick any schema in the `iceberg` catalog.

## Oracle

The container has no Oracle driver yet. It also has SQLAlchemy 1.4.54, whose Oracle support expects the older `cx_Oracle` package. The usual workaround is to install the modern `oracledb` driver (no Oracle client libraries needed) and register it under the old name.

1. Create `~/testsuperset/superset/docker/requirements-local.txt` containing:
   ```
   oracledb
   ```
   The container's startup script installs whatever is in this file each time it starts.
2. Add these lines to `docker/pythonpath_dev/superset_config_docker.py`:
   ```python
   import sys
   import oracledb

   oracledb.version = "8.3.0"
   sys.modules["cx_Oracle"] = oracledb
   ```
3. Restart the Superset containers so they install the driver and reload the config:
   ```bash
   cd ~/testsuperset/superset && docker compose restart superset superset-worker superset-worker-beat
   ```
4. Use this URI:
   ```
   oracle+cx_oracle://superset:superset@host.docker.internal:1521/?service_name=FREEPDB1
   ```
   Write `FREEPDB1` as `?service_name=`, not as `/FREEPDB1`. In the path, SQLAlchemy treats it as a SID and the connection is refused.

## Adding them in Superset

1. Open Superset at http://localhost:8088.
2. Go to **Settings → Database Connections → + Database**.
3. Choose **Trino**, or **Other** for Oracle, and switch to entering a SQLAlchemy URI if the form asks.
4. Paste the URI, give it a display name and click **Test connection**.
5. Under **Advanced → SQL Lab**, tick **Expose database in SQL Lab**. Leave DML off.
6. Click **Connect**.

In Oracle, Superset only lists the schemas the `superset` user can see. If geonames, gleif and the others don't appear, that user needs `SELECT` grants on them.
