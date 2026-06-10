#!/bin/bash
# ============================================================
# sync_checkmk_cache.sh
#
# Copies CheckMK cache files into a versioned archive on
# every filesystem write event.
#
# Optimiert für hohe Last (1600+ Files/Minute):
#   - Batch-Verarbeitung via temporäre Queue-Datei
#   - Parallele Copies mit xargs -P
#   - flock verhindert Race Condition beim Stundenwechsel
#   - inotify Queue-Größe wird automatisch erhöht
#   - IN_Q_OVERFLOW wird erkannt und geloggt
#
# Structure:
#   $DEST_DIR/
#     2026-05-04_13/            <- current hour: raw uncompressed folder
#       2026-05-04_13-22-01.hostname
#       2026-05-04_13-45-10.hostname
#     2026-05-04_12.tar.zst      <- past hours: one tar.zst per hour
#     2026-05-04_11.tar.zst
#     ...
#
# Configuration via EnvironmentFile (systemd):
#   /etc/sysconfig/sync_checkmk_cache_<site>
#
# Required variables:
#   OMD_SITE            CheckMK site name
#   SOURCE_DIR          Source directory (CheckMK cache)
#   DEST_DIR            Destination directory for archive
#   RETENTION_DAYS      Number of days to keep archives
#   LOGFILE             Path to log file
#   ZSTD_LEVEL          zstd compression level (1=fastest, 3=default)
#   BATCH_WORKERS       Parallel cp processes
#   BATCH_SIZE          Files per worker call
#   BATCH_FLUSH_INTERVAL  Seconds between flush cycles
#   LOCKDIR             Directory for per-bucket lock files
#
# Dependencies:
#   sudo yum install inotify-tools   (RHEL/CentOS)
#   sudo yum install zstd             (RHEL/CentOS)
# ============================================================

_required_vars=(
    OMD_SITE SOURCE_DIR DEST_DIR RETENTION_DAYS LOGFILE
    ZSTD_LEVEL BATCH_WORKERS BATCH_SIZE BATCH_FLUSH_INTERVAL LOCKDIR
)
for _var in "${_required_vars[@]}"; do
    if [[ -z "${!_var}" ]]; then
        echo "ERROR: Required variable \$$_var is not set." \
             "Source the EnvironmentFile before running this script." >&2
        exit 1
    fi
done
unset _required_vars _var

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"; }

current_hour_bucket() { date '+%Y-%m-%d_%H'; }

purge_old_versions() {
    log "Purge: Removing archives older than ${RETENTION_DAYS} days in $DEST_DIR ..."
    local count=0
    local cutoff_epoch
    cutoff_epoch=$(date -d "${RETENTION_DAYS} days ago" '+%s')
    while IFS= read -r -d '' archive; do
        local base
        base=$(basename "$archive" .tar.zst)
        if [[ ! "$base" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})_([0-9]{2})$ ]]; then continue; fi
        local arc_date="${BASH_REMATCH[1]}" arc_hour="${BASH_REMATCH[2]}" arc_epoch
        arc_epoch=$(date -d "${arc_date} ${arc_hour}:00:00" '+%s' 2>/dev/null) || continue
        if [[ "$arc_epoch" -lt "$cutoff_epoch" ]]; then
            rm -f "$archive"
            log "Purge: Deleted -> $archive"
            (( count++ ))
        fi
    done < <(find "$DEST_DIR" -mindepth 1 -maxdepth 1 -type f \
                  -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9].tar.zst' -print0)
    log "Purge: ${count} archives removed."
}

compress_old_hours() {
    local current count=0
    current=$(current_hour_bucket)
    log "Compress: Packing past hourly folders ..."
    while IFS= read -r -d '' hour_dir; do
        local dir_name
        dir_name=$(basename "$hour_dir")
        [[ "$dir_name" == "$current" ]] && continue
        local archive="${DEST_DIR}/${dir_name}.tar.zst"
        if [[ -f "$archive" ]]; then
            log "Compress: Skipping $dir_name (archive already exists)"
            continue
        fi
        local bucket_lock="${LOCKDIR}/${dir_name}.lock"
        (
            flock -x 200
            if tar -cf - -C "$DEST_DIR" "$dir_name" | zstd -T0 -"${ZSTD_LEVEL}" -o "$archive"; then
                rm -rf "$hour_dir"
                log "Compress: $dir_name/ -> ${dir_name}.tar.zst"
            else
                rm -f "$archive"
                log "ERR  tar failed for: $dir_name"
            fi
        ) 200>"$bucket_lock"
        (( count++ ))
    done < <(find "$DEST_DIR" -mindepth 1 -maxdepth 1 -type d \
                  -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9]' -print0)
    log "Compress: ${count} folders packed."
}

QUEUE_FILE=$(mktemp /tmp/checkmk_queue_XXXXXX)
QUEUE_LOCK=$(mktemp /tmp/checkmk_qlock_XXXXXX)
STOP_FILE=$(mktemp /tmp/checkmk_stop_XXXXXX)
rm -f "$STOP_FILE"

batch_copy_worker() {
    log "BatchWorker: started (workers=${BATCH_WORKERS}, batch_size=${BATCH_SIZE})"
    while true; do
        sleep "$BATCH_FLUSH_INTERVAL"
        local batch_file
        batch_file=$(mktemp /tmp/checkmk_batch_XXXXXX)
        (
            flock -x 201
            cp "$QUEUE_FILE" "$batch_file"
            true > "$QUEUE_FILE"
        ) 201>"$QUEUE_LOCK"

        if [[ ! -s "$batch_file" ]]; then
            rm -f "$batch_file"
            [[ -f "$STOP_FILE" ]] && break
            continue
        fi

        local line_count ts bucket hour_dir bucket_lock
        line_count=$(wc -l < "$batch_file")
        ts=$(date '+%Y-%m-%d_%H-%M-%S')
        bucket=$(current_hour_bucket)
        hour_dir="${DEST_DIR}/${bucket}"
        bucket_lock="${LOCKDIR}/${bucket}.lock"
        (
            flock -s 200
            mkdir -p "$hour_dir"
            xargs -d '\n' -P "$BATCH_WORKERS" -n "$BATCH_SIZE" \
                bash -c '
                    ts="$0"; hour_dir="$2"; source_dir="$3"
                    shift 3
                    for filepath in "$@"; do
                        [[ -f "$filepath" ]] || continue
                        rel="${filepath#${source_dir}/}"
                        safe="${rel//\//.}"
                        cp --preserve=timestamps "$filepath" \
                           "${hour_dir}/${ts}.${safe}" \
                            || echo "ERR  Failed: $filepath" >&2
                    done
                ' "$ts" "$bucket" "$hour_dir" "$SOURCE_DIR" < "$batch_file"
        ) 200>"$bucket_lock"

        log "Batch: flushed ${line_count} files -> ${bucket}/"
        rm -f "$batch_file"
        [[ -f "$STOP_FILE" ]] && break
    done
    log "BatchWorker: stopped."
}

# ── Preflight ────────────────────────────────────────────────────────────────
command -v inotifywait &>/dev/null || { echo "ERROR: inotifywait not found." >&2; exit 1; }
command -v zstd        &>/dev/null || { echo "ERROR: zstd not found."        >&2; exit 1; }
[[ -d "$SOURCE_DIR" ]]             || { echo "ERROR: SOURCE_DIR not found: $SOURCE_DIR" >&2; exit 1; }
mkdir -p "$DEST_DIR" "$LOCKDIR"    || { echo "ERROR: Cannot create DEST_DIR/LOCKDIR." >&2; exit 1; }
touch "$LOGFILE" 2>/dev/null

current_max=$(cat /proc/sys/fs/inotify/max_queued_events 2>/dev/null || echo 0)
if [[ "$current_max" -lt 131072 ]]; then
    if sysctl -w fs.inotify.max_queued_events=131072 2>/dev/null; then
        log "Preflight: inotify max_queued_events set to 131072"
    else
        log "WARN  Could not raise inotify max_queued_events (not root?), current=${current_max}"
    fi
fi

log "========================================="
log "Watcher started (high-throughput mode)"
log "  Site         : $OMD_SITE"
log "  Source       : $SOURCE_DIR"
log "  Destination  : $DEST_DIR"
log "  Retention    : ${RETENTION_DAYS} days"
log "  Workers      : ${BATCH_WORKERS} parallel, ${BATCH_SIZE} files/batch"
log "  Flush        : every ${BATCH_FLUSH_INTERVAL}s"
log "========================================="

purge_old_versions
compress_old_hours
log "Startup: queuing existing files for initial snapshot..."
find "$SOURCE_DIR" -maxdepth 1 -type f -print >> "$QUEUE_FILE"

# ── Background: hourly timer ─────────────────────────────────────────────────
(
    while true; do
        now=$(date '+%s')
        next_hour_epoch=$(date -d "$(date -d '+1 hour' '+%Y-%m-%d %H'):00:02" '+%s')
        [[ "$next_hour_epoch" -le "$now" ]] && next_hour_epoch=$(( now + 3600 ))
        sleep $(( next_hour_epoch - now ))
        purge_old_versions
        sleep 300
        compress_old_hours
    done
) &
TIMER_PID=$!

batch_copy_worker &
WORKER_PID=$!

trap 'rm -f "$QUEUE_FILE" "$QUEUE_LOCK" "$STOP_FILE"' EXIT
trap '
    log "Shutdown: draining queue before exit..."
    touch "'"$STOP_FILE"'"
    wait "'"$WORKER_PID"'" 2>/dev/null
    kill "'"$TIMER_PID"'" 2>/dev/null
    log "Watcher stopped."
    exit 0
' SIGTERM SIGINT

# ── Main loop: inotify → queue ───────────────────────────────────────────────
inotifywait --monitor --recursive --quiet \
    --format '%e %w%f' \
    --event close_write --event moved_to \
    "$SOURCE_DIR" |
while IFS= read -r line; do
    if [[ "$line" == *"Q_OVERFLOW"* ]]; then
        log "WARN  IN_Q_OVERFLOW: inotify queue overflowed, some events lost!"
        log "      Consider raising fs.inotify.max_queued_events further."
        continue
    fi
    filepath="${line#* }"
    [[ -f "$filepath" ]] || continue
    (
        flock -x 201
        echo "$filepath" >> "$QUEUE_FILE"
    ) 201>"$QUEUE_LOCK"
done
