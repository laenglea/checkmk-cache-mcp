# checkmk-cache-mcp

Two-component system that archives CheckMK agent-output files and exposes them via an [MCP](https://modelcontextprotocol.io/) server — enabling AI assistants to query live and historical host data.

```
CheckMK agent cache  →  Collector (inotify)  →  Hourly tar.zst archives
                                               ↓
                                         MCP Server (FastAPI/SSE)
                                               ↓
                                         Claude / any MCP client
```

## Components

| Component | Path | Description |
|-----------|------|-------------|
| **Collector** | `collector/sync_checkmk_cache.sh` | inotify watcher, batches copies into versioned hourly archives (tar.zst) |
| **MCP Server** | `mcp/checkmk_cache_mcp.py` | FastAPI/SSE server exposing 4 current + 3 historical MCP tools |

## Requirements

- RHEL / CentOS / Rocky 8+ (or any Linux with systemd)
- `inotify-tools` (`inotifywait`)
- `zstd`
- Python 3.11+

## Quick install

```bash
git clone https://github.com/laenglea/checkmk-cache-mcp.git
cd checkmk-cache-mcp
sudo ./install.sh mysite          # replace mysite with your OMD site name
```

Then edit the two config files printed by the installer and start the services:

```bash
sudo systemctl start checkmk-cache-collector@mysite
sudo systemctl start checkmk-cache-mcp
```

## Configuration

### Collector — `/etc/sysconfig/sync_checkmk_cache_<site>`

| Variable | Default | Description |
|----------|---------|-------------|
| `OMD_SITE` | — | CheckMK site name (for log output) |
| `SOURCE_DIR` | — | CheckMK agent-output cache directory |
| `DEST_DIR` | — | Archive destination |
| `RETENTION_DAYS` | — | Days to keep hourly archives |
| `LOGFILE` | — | Log file path |
| `ZSTD_LEVEL` | `1` | zstd compression level (1=fastest) |
| `BATCH_WORKERS` | `4` | Parallel cp workers |
| `BATCH_SIZE` | `50` | Files per worker call |
| `BATCH_FLUSH_INTERVAL` | `5` | Seconds between flush cycles |
| `LOCKDIR` | — | Directory for per-hour flock files |

See [`collector/sync_checkmk_cache.env.example`](collector/sync_checkmk_cache.env.example) for a complete example.

### MCP Server — `/etc/sysconfig/checkmk-cache-mcp`

| Variable | Default | Description |
|----------|---------|-------------|
| `CHECKMK_CACHE_DIR` | `/cachehistory/prodhubli` | Directory with current host files |
| `CHECKMK_HISTORY_DIR` | _(empty)_ | Archive directory; leave empty to disable historical tools |
| `CHECKMK_FILE_TZ` | `Europe/Vaduz` | Timezone of archive filenames |
| `MCP_HOST` | `0.0.0.0` | Listen address |
| `MCP_PORT` | `8765` | Listen port |
| `MCP_API_KEY` | _(empty)_ | Bearer / X-Api-Key auth; empty = no auth |

See [`mcp/checkmk_cache_mcp.env.example`](mcp/checkmk_cache_mcp.env.example) for a complete example.

## MCP Tools

### Current data

| Tool | Description |
|------|-------------|
| `list_hosts` | Lists all hosts with cache-file age |
| `get_overview` | OS, uptime, CPU, RAM, top-5 disks — single call |
| `get_processes` | Process list, server-side sorted/filtered; auto-detects Windows/Linux |
| `get_section` | Raw CheckMK section content (with aliases like `mem`, `wincpu`, …) |

### Historical data (requires `CHECKMK_HISTORY_DIR`)

| Tool | Description |
|------|-------------|
| `get_history_overview` | Overview for a host at a specific timestamp |
| `get_history_processes` | Process list at a specific timestamp |
| `get_history_section` | Raw section at a specific timestamp |

## Archive structure

```
$DEST_DIR/
  2026-05-04_13/                    ← current hour (raw files)
    2026-05-04_13-22-01.hostname
    2026-05-04_13-45-10.hostname
  2026-05-04_12.tar.zst             ← past hours (compressed)
  2026-05-04_11.tar.zst
  ...
```

Each file is named `<timestamp>.<hostname>`, one snapshot per flush cycle per host.

## Multiple CheckMK sites

The collector is an instantiated systemd service (`checkmk-cache-collector@.service`). Run one instance per site:

```bash
sudo systemctl enable --now checkmk-cache-collector@site1
sudo systemctl enable --now checkmk-cache-collector@site2
```

Each instance needs its own EnvironmentFile: `/etc/sysconfig/sync_checkmk_cache_site1`, etc.

The MCP server is a single process — point `CHECKMK_CACHE_DIR` and `CHECKMK_HISTORY_DIR` at whichever site you want to expose, or run multiple MCP instances on different ports.

## Ops

```bash
# Live logs
journalctl -u checkmk-cache-collector@mysite -f
journalctl -u checkmk-cache-mcp -f

# Health check
curl http://localhost:8765/health

# Manual compress + purge (e.g. after downtime)
source /etc/sysconfig/sync_checkmk_cache_mysite
/opt/checkmk-cache-mcp/collector/sync_checkmk_cache.sh  # runs normally
# or call compress_old_hours / purge_old_versions directly by sourcing the script

# Update
cd checkmk-cache-mcp && git pull
sudo ./install.sh mysite
sudo systemctl restart checkmk-cache-collector@mysite checkmk-cache-mcp
```

## License

MIT
