#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v nmcli >/dev/null 2>&1; then
    printf 'NetworkManager is required. See the Raspberry Pi hotspot setup in README.md.\n' >&2
    exit 1
fi

if ! address="$(LC_ALL=C nmcli --get-values ipv4.addresses connection show id oak-ffc-hotspot)"; then
    printf 'Configure the hotspot first: sudo python3 scripts/hotspot.py configure\n' >&2
    exit 1
fi
hotspot_ip="${address%/*}"
if [[ ! "$address" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/24$ ]]; then
    printf 'Unexpected hotspot address. Check: python3 scripts/hotspot.py status\n' >&2
    exit 1
fi
IFS=. read -r -a octets <<< "$hotspot_ip"
for octet in "${octets[@]}"; do
    if (( 10#$octet > 255 )); then
        printf 'Unexpected hotspot address. Check: python3 scripts/hotspot.py status\n' >&2
        exit 1
    fi
done

# Keep all application options available, including demo mode and custom ports.
port=8080
expect_port=false
for argument in "$@"; do
    if "$expect_port"; then
        port="$argument"
        expect_port=false
    elif [[ "$argument" == --port ]]; then
        expect_port=true
    elif [[ "$argument" == --port=* ]]; then
        port="${argument#--port=}"
    fi
done

printf 'Connect to the configured hotspot and open http://%s:%s\n' "$hotspot_ip" "$port"
exec bash "$project_dir/scripts/run.sh" --host 0.0.0.0 "$@"
