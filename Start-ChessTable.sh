#!/usr/bin/env bash
# Start the checkpointed chess-table GUI on Linux or WSL.
set -euo pipefail

usage() {
    cat <<'HELP'
Usage: ./Start-ChessTable.sh [options]

Start a new simulation by default. Physical mode connects to an Uno and may
move the board after you start play in the GUI or through Aina.

Options:
  --mode simulation|physical   Game mode (default: simulation)
  --physical                  Shortcut for --mode physical
  --resume PATH               Resume an existing checkpoint; requires --mode
  --state PATH                Path for a new checkpoint (default: unique runs/ path)
  --port /dev/...             Uno serial device; physical mode auto-detects when omitted
  --enable-aina              Enable the authenticated local Aina API
  --aina-token-file PATH      Override ~/.config/chess-table/aina.token
  --gui-port PORT             Browser GUI port (default: 8765)
  --aina-port PORT            Aina API port (default: 8766)
  --engine PATH               Override Stockfish executable or directory
  --full-home                 Request full HOME (physical mode only)
  --dry-run                   Validate and show settings without starting the host
  -h, --help                  Show this help

Examples:
  ./Start-ChessTable.sh --enable-aina
  ./Start-ChessTable.sh --mode physical --enable-aina
  ./Start-ChessTable.sh --mode physical --resume runs/chess-physical-EXAMPLE.json

Run from any directory. Relative checkpoint paths are resolved from computer/.
Stockfish defaults to ~/Documents/Stockfish or ~/Documents/stockfish if present.
WSL also checks the Windows user's Documents/Stockfish and Documents/stockfish.
If no downloaded engine is found, Linux also checks /usr/games/stockfish.
In WSL, an unattached, identified Uno is attached through usbipd-win on a real
physical launch. --dry-run only checks usbipd state; it never attaches USB.
The launcher does not install dependencies, bind USB, or start Aina's companion.
HELP
}

die() {
    printf 'Chess table: %s\n' "$*" >&2
    exit 1
}

require_value() {
    local option=$1 value=${2-}
    [[ -n $value && $value != --* ]] || die "$option requires a value. See --help."
}

validate_port_number() {
    local option=$1 value=$2
    [[ $value =~ ^[0-9]+$ ]] || die "$option must be a number from 1 to 65535."
    (( 10#$value >= 1 && 10#$value <= 65535 )) || die "$option must be from 1 to 65535."
}

# Recognize the Uno's USB VID:PID even when udev has not made a by-id link.
# The sysfs root parameter lets the serial selector be tested without hardware.
is_uno_usb_device() {
    local tty_name=$1 sys_root=${2:-/sys} dir vendor product
    dir=$(readlink -f -- "$sys_root/class/tty/$tty_name/device") || return 1
    while [[ $dir == "$sys_root"/* ]]; do
        if [[ -r $dir/idVendor && -r $dir/idProduct ]]; then
            read -r vendor < "$dir/idVendor"
            read -r product < "$dir/idProduct"
            [[ ${vendor,,}:${product,,} == 2341:0043 ]] && return 0
        fi
        dir=${dir%/*}
    done
    return 1
}

# Print the only identified Uno. Prefer a stable by-id path. Generic USB
# serial devices require an explicit --port, even if only one is attached.
discover_serial_port() {
    local root=${1:-/dev} sys_root=${2:-/sys} path target link name
    local -a raw_ports=() uno_ports=()
    local -A raw_seen=() stable_for_raw=() uno_for_raw=()

    for path in "$root"/ttyACM* "$root"/ttyUSB*; do
        [[ -e $path ]] || continue
        raw_ports+=("$path")
        raw_seen["$path"]=1
        if is_uno_usb_device "${path##*/}" "$sys_root"; then
            uno_for_raw["$path"]=$path
        fi
    done
    for link in "$root"/serial/by-id/*; do
        [[ -L $link ]] || continue
        target=$(readlink -f -- "$link") || continue
        [[ -n ${raw_seen["$target"]+yes} ]] || continue
        [[ -n ${stable_for_raw["$target"]+yes} ]] || stable_for_raw["$target"]=$link
        name=${link##*/}
        name=${name,,}
        if [[ $name == *arduino* || $name == *uno* ]]; then
            uno_for_raw["$target"]=$link
        fi
    done

    for path in "${raw_ports[@]}"; do
        [[ -n ${uno_for_raw["$path"]+yes} ]] || continue
        if [[ -n ${stable_for_raw["$path"]+yes} ]]; then
            # A labelled by-id name is best; a generic stable name still beats
            # a changing tty number when sysfs identifies the USB device.
            if [[ ${uno_for_raw["$path"]} == "$root"/serial/by-id/* ]]; then
                uno_ports+=("${uno_for_raw["$path"]}")
            else
                uno_ports+=("${stable_for_raw["$path"]}")
            fi
        else
            uno_ports+=("${uno_for_raw["$path"]}")
        fi
    done

    if (( ${#uno_ports[@]} == 1 )); then
        printf '%s\n' "${uno_ports[0]}"
    elif (( ${#uno_ports[@]} > 1 )); then
        printf 'Multiple Arduino/Uno serial devices found:\n' >&2
        printf '  %s\n' "${uno_ports[@]}" >&2
        return 2
    elif (( ${#raw_ports[@]} == 0 )); then
        printf 'No Uno serial device found. Attach USB to Linux/WSL, then check /dev/serial/by-id or /dev/ttyACM*.\n' >&2
        return 1
    else
        printf 'No Uno was identified by its by-id name or USB ID 2341:0043. Choose the attached Uno with --port:\n' >&2
        printf '  %s\n' "${raw_ports[@]}" >&2
        return 2
    fi
}

assert_loopback_port_available() {
    local python=$1 port=$2 purpose=$3 option=$4
    if ! "$python" -c '
import socket, sys
try:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
' "$port"; then
        die "$purpose port $port is unavailable. Stop the existing listener or choose another port with $option. No new game was started."
    fi
}

is_wsl() {
    [[ -n ${WSL_DISTRO_NAME:-} && -r /proc/sys/kernel/osrelease ]] &&
        grep -qi microsoft /proc/sys/kernel/osrelease
}

# On WSL, use the signed-in Windows user's Stockfish download if the Linux
# home has no copy. Convert the real profile path rather than guessing /mnt/c.
find_windows_stockfish_in_wsl() {
    local windows_profile linux_profile candidate
    is_wsl || return 1
    command -v powershell.exe >/dev/null 2>&1 || return 1
    command -v wslpath >/dev/null 2>&1 || return 1
    windows_profile=$(powershell.exe -NoProfile -NonInteractive -Command '$env:USERPROFILE' 2>/dev/null) || return 1
    windows_profile=${windows_profile//$'\r'/}
    [[ $windows_profile =~ ^[A-Za-z]:\\ && $windows_profile != *$'\n'* ]] || return 1
    linux_profile=$(wslpath -u "$windows_profile" 2>/dev/null) || return 1
    [[ $linux_profile == /* ]] || return 1
    for candidate in "$linux_profile/Documents/Stockfish" "$linux_profile/Documents/stockfish"; do
        if [[ -d $candidate ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# usbipd-win's JSON lets us target exactly one connected Uno without parsing
# localized display text or selecting another USB device. A persisted but
# unplugged Uno has a null BusId and cannot be attached.
usbipd_uno_state() {
    local python=$1 state_json
    command -v usbipd.exe >/dev/null 2>&1 || die 'usbipd.exe is unavailable in WSL. Install usbipd-win on Windows and enable WSL interop.'
    state_json=$(usbipd.exe state) || die 'Could not read usbipd state. Check usbipd-win in Windows.'
    "$python" -c '
import json, re, sys
try:
    devices = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))["Devices"]
except (ValueError, KeyError, TypeError):
    print("error")
    sys.exit(0)
matches = [d for d in devices
           if re.search(r"VID_2341&PID_0043", str(d.get("InstanceId", "")), re.I)
           and d.get("BusId")]
if len(matches) > 1:
    print("ambiguous")
elif not matches:
    print("missing")
else:
    device = matches[0]
    print(("attached" if device.get("ClientIPAddress") else "ready")
          + "\t" + device["BusId"])
' <<< "$state_json"
}

# Return a Linux serial path after attaching, or pending:BUSID during a dry
# run. Never binds/unbinds USB: the one-time sharing step stays in Windows.
auto_attach_wsl_uno() {
    local python=$1 dry_run=$2 dev_root=${3:-/dev} sys_root=${4:-/sys}
    local state bus_id selected_port attempt
    state=$(usbipd_uno_state "$python") || return 1
    case $state in
        ready$'\t'*) bus_id=${state#*$'\t'} ;;
        missing) die 'No connected Windows Uno (USB ID 2341:0043) is available for WSL attachment. Plug it in, then retry.' ;;
        ambiguous) die 'Multiple Windows Uno devices match USB ID 2341:0043. Attach the intended one manually, then specify --port.' ;;
        attached$'\t'*) die 'Windows reports the Uno already attached, but Linux has no identified serial device. Check this WSL distribution and /dev/serial/by-id.' ;;
        *) die 'Could not interpret usbipd state. Check usbipd-win on Windows.' ;;
    esac

    if (( dry_run == 1 )); then
        printf 'pending:%s\n' "$bus_id"
        return 0
    fi

    printf 'Attaching Uno on Windows USB bus %s to WSL %s...\n' "$bus_id" "$WSL_DISTRO_NAME" >&2
    if ! usbipd.exe attach --busid "$bus_id" --wsl "$WSL_DISTRO_NAME" 1>&2; then
        die "USB attach failed. In elevated Windows PowerShell, run: usbipd bind --busid $bus_id; then retry."
    fi
    for (( attempt=0; attempt<30; attempt++ )); do
        if selected_port=$(discover_serial_port "$dev_root" "$sys_root" 2>/dev/null); then
            printf '%s\n' "$selected_port"
            return 0
        fi
        sleep 0.5
    done
    die 'usbipd attached the Uno, but no identified serial device appeared in Linux. Check /dev/serial/by-id and WSL USB support.'
}

main() {
    local script_dir computer_dir python geometry mode=simulation mode_set=0
    local resume=0 state='' state_set=0 port='' enable_aina=0 aina_token_file=''
    local gui_port=8765 aina_port=8766 engine='' full_home=0 dry_run=0
    local selected_port checkpoint_kind stamp suffix status
    local -a host_args=()

    while (( $# > 0 )); do
        case $1 in
            --mode)
                require_value "$1" "${2-}"
                mode=$2; mode_set=1; shift 2 ;;
            --physical)
                mode=physical; mode_set=1; shift ;;
            --resume)
                require_value "$1" "${2-}"
                (( state_set == 0 )) || die 'Use --resume PATH without --state.'
                resume=1; state=$2; shift 2 ;;
            --state)
                require_value "$1" "${2-}"
                (( resume == 0 )) || die 'Use --resume PATH without --state.'
                state_set=1; state=$2; shift 2 ;;
            --port)
                require_value "$1" "${2-}"
                port=$2; shift 2 ;;
            --enable-aina)
                enable_aina=1; shift ;;
            --aina-token-file)
                require_value "$1" "${2-}"
                aina_token_file=$2; shift 2 ;;
            --gui-port)
                require_value "$1" "${2-}"
                gui_port=$2; shift 2 ;;
            --aina-port)
                require_value "$1" "${2-}"
                aina_port=$2; shift 2 ;;
            --engine)
                require_value "$1" "${2-}"
                engine=$2; shift 2 ;;
            --full-home)
                full_home=1; shift ;;
            --dry-run)
                dry_run=1; shift ;;
            -h|--help)
                usage; return 0 ;;
            *)
                die "Unknown option: $1. See --help." ;;
        esac
    done

    [[ $mode == simulation || $mode == physical ]] || die '--mode must be simulation or physical.'
    (( resume == 0 || mode_set == 1 )) || die 'Choose --mode simulation or --mode physical explicitly when resuming.'
    [[ -z $port || $mode == physical ]] || die '--port is only valid with --mode physical.'
    (( full_home == 0 )) || [[ $mode == physical ]] || die '--full-home is only valid with --mode physical.'
    [[ -z $aina_token_file || $enable_aina == 1 ]] || die '--aina-token-file requires --enable-aina.'
    validate_port_number --gui-port "$gui_port"
    validate_port_number --aina-port "$aina_port"
    gui_port=$((10#$gui_port))
    aina_port=$((10#$aina_port))
    (( enable_aina == 0 )) || [[ $gui_port != "$aina_port" ]] || die '--gui-port and --aina-port must differ.'

    script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
    computer_dir=$script_dir/computer
    [[ -d $computer_dir ]] || die "Cannot find computer/ beside $script_dir."
    python=$computer_dir/.venv/bin/python
    [[ -x $python ]] || die "Python environment missing at $python. Create computer/.venv and install computer/requirements.txt."

    if [[ -z $state ]]; then
        checkpoint_kind=sim
        [[ $mode == physical ]] && checkpoint_kind=physical
        stamp=$(date +%Y%m%d-%H%M%S)
        suffix=$("$python" -c 'import secrets; print(secrets.token_hex(4))')
        state="runs/chess-$checkpoint_kind-$stamp-$suffix.json"
    fi
    if [[ $state != /* ]]; then
        state=$computer_dir/$state
    fi
    state=$(realpath -m -- "$state")
    if (( resume == 1 )); then
        [[ -f $state ]] || die "Cannot resume: checkpoint does not exist at $state."
        case ${state##*/} in
            chess-sim-*) [[ $mode == simulation ]] || die 'Checkpoint filename indicates simulation; choose --mode simulation.' ;;
            chess-physical-*) [[ $mode == physical ]] || die 'Checkpoint filename indicates physical; choose --mode physical.' ;;
        esac
    else
        [[ ! -e $state ]] || die "Checkpoint already exists at $state. Use --resume with an explicit --mode."
    fi

    if (( enable_aina == 1 )); then
        [[ -n $aina_token_file ]] || aina_token_file=$HOME/.config/chess-table/aina.token
        [[ $aina_token_file == /* ]] || die '--aina-token-file must be an absolute Linux path.'
        [[ -r $aina_token_file && -f $aina_token_file ]] || die "Aina token file is not readable at $aina_token_file. See docs/REMOTE_AINA.md."
    fi
    assert_loopback_port_available "$python" "$gui_port" GUI --gui-port
    if (( enable_aina == 1 )); then
        assert_loopback_port_available "$python" "$aina_port" 'Aina API' --aina-port
    fi

    host_args=(-m chess_table_host.gui)
    if (( resume == 1 )); then host_args+=(--resume); else host_args+=(--new); fi
    host_args+=(--state "$state" --http-port "$gui_port")
    if [[ $mode == simulation ]]; then
        geometry=$computer_dir/fixtures/board_geometry_2026-10-05.json
        [[ -f $geometry ]] || die "Simulation geometry fixture is missing at $geometry."
        host_args+=(--simulate --geometry-file "$geometry")
    else
        if [[ -z $port ]]; then
            if selected_port=$(discover_serial_port 2>/dev/null); then
                port=$selected_port
            elif is_wsl; then
                port=$(auto_attach_wsl_uno "$python" "$dry_run") || return 1
            else
                die 'No identified Uno serial device found. Attach it or specify --port /dev/serial/by-id/... or --port /dev/ttyACM0.'
            fi
        fi
        if [[ $port == pending:* ]]; then
            (( dry_run == 1 )) || die 'Internal error: pending USB attachment outside dry-run.'
        else
            [[ $port == /dev/* ]] || die '--port must be an absolute /dev path.'
            [[ -c $port ]] || die "Serial device $port is unavailable. Attach the Uno to Linux/WSL or choose --port again."
            [[ -r $port && -w $port ]] || die "Cannot read and write $port. Check device permissions (often the dialout group), then reconnect."
            host_args+=(--port "$port")
        fi
        (( full_home == 0 )) || host_args+=(--home)
    fi
    if [[ -z $engine ]]; then
        if [[ -d $HOME/Documents/Stockfish ]]; then
            engine=$HOME/Documents/Stockfish
        elif [[ -d $HOME/Documents/stockfish ]]; then
            engine=$HOME/Documents/stockfish
        elif engine=$(find_windows_stockfish_in_wsl); then
            :
        elif [[ -x /usr/games/stockfish ]]; then
            engine=/usr/games/stockfish
        fi
    fi
    [[ -z $engine ]] || host_args+=(--engine "$engine")

    if (( enable_aina == 1 )); then
        host_args+=(--remote-token-file "$aina_token_file" --remote-http-port "$aina_port")
    fi

    printf 'Checkpoint: %s\n' "$state"
    printf 'Mode: %s\n' "$mode"
    if [[ -n $engine ]]; then
        printf 'Stockfish: %s\n' "$engine"
    fi
    printf 'Local GUI: http://127.0.0.1:%s/\n' "$gui_port"
    if (( enable_aina == 1 )); then
        printf 'Aina API: http://127.0.0.1:%s/v1/status\n' "$aina_port"
        printf 'Aina token file: %s (value hidden)\n' "$aina_token_file"
    fi
    if [[ $mode == physical ]]; then
        if [[ $port == pending:* ]]; then
            printf 'Uno USB: bus %s will attach to WSL on the actual run.\n' "${port#pending:}"
        else
            printf 'Uno port: %s; keep the power cutoff reachable.\n' "$port"
        fi
    fi
    if (( dry_run == 1 )); then
        printf 'Dry run: host was not started.\n'
        return 0
    fi

    cd -- "$computer_dir"
    if "$python" "${host_args[@]}"; then
        return 0
    else
        status=$?
        die "Chess host exited with code $status. Preserve the checkpoint and inspect the error above."
    fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
