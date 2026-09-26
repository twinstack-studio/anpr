#!/usr/bin/env bash
# Clears all jobs and re-processes the demo samples shown on the public page.
set -euo pipefail
cd "$(dirname "$0")/.."
API=http://127.0.0.1:9517
DB="psql -h /var/run/postgresql anpr -Atq"
$DB -c "delete from jobs"
rm -rf runtime/uploads/* runtime/results/*

add() {  # file, gate, title, minutes-ago
  local id
  id=$(curl -sf -F "file=@$1" -F "gate=$2" $API/api/jobs | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
  $DB -c "update jobs set sample=true, filename='$3', created_at=now() - interval '$4 minutes' where id='$id'"
  echo "$id  $3"
}
add samples/lahore-traffic.mp4 entry "Lahore street traffic" 0
add samples/photos/photo-0.jpg none "Sindh plate · Corolla" 1
add samples/photos/photo-1.jpg none "Sindh plate · Chevrolet" 2
add samples/photos/photo-2.jpg none "Punjab plate · Suzuki" 3
add samples/photos/photo-3.jpg none "Islamabad plate · Corolla" 4
add samples/photos/photo-4.jpg none "Motorbike plate" 5
