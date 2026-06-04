#!/bin/sh

BASE_PATH="/var/apps/fn-trim.vm/target/www"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REQUEST_METHOD="$(printf '%s' "${REQUEST_METHOD:-GET}" | tr '[:lower:]' '[:upper:]')"

BODY_TMP=""
HDR_TMP=""
OUT_BODY=""

print_header() {
  printf '%s\r\n' "$1"
}

cleanup() {
  rm -f "$BODY_TMP" "$HDR_TMP" "$OUT_BODY"
}

trim_header_value() {
  local value="$1"
  value="${value#*:}"
  value="${value#"${value%%[![:space:]]*}"}"
  printf '%s' "${value%$'\r'}"
}

send_text_response() {
  local status="$1"
  local body="${2:-}"

  print_header "Status: $status"
  print_header "Content-Type: text/plain; charset=utf-8"
  print_header "Content-Length: ${#body}"
  print_header ""

  if [ "$REQUEST_METHOD" != "HEAD" ] && [ -n "$body" ]; then
    printf '%s' "$body"
  fi
  exit 0
}

send_json_response() {
  local status="$1"
  local body="$2"

  print_header "Status: $status"
  print_header "Content-Type: application/json; charset=utf-8"
  print_header "Content-Length: ${#body}"
  print_header ""

  if [ "$REQUEST_METHOD" != "HEAD" ]; then
    printf '%s' "$body"
  fi
  exit 0
}

send_empty_response() {
  print_header "Status: $1"
  print_header ""
  exit 0
}

create_temp_file() {
  mktemp 2>/dev/null || return 1
}

resolve_rel_path() {
  local uri_no_query rel

  uri_no_query="${REQUEST_URI%%\?*}"
  rel="/"
  case "$uri_no_query" in
    *index.cgi*)
      rel="${uri_no_query#*index.cgi}"
      ;;
  esac

  if [ -z "$rel" ] || [ "$rel" = "/" ]; then
    rel="/index.html"
  fi

  if [ "${rel#/}" = "$rel" ]; then
    rel="/$rel"
  fi

  case "$rel" in
    */)
      rel="${rel}index.html"
      ;;
  esac

  printf '%s' "$rel"
}

is_path_traversal() {
  local path="$1"

  case "$path" in
    ../* | */../* | */.. | ..)
      return 0
      ;;
  esac

  return 1
}

detect_mime() {
  local file_path="$1"
  local ext="${file_path##*.}"
  local ext_lc

  ext_lc="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"
  case "$ext_lc" in
    html | htm) printf '%s' "text/html; charset=utf-8" ;;
    css) printf '%s' "text/css; charset=utf-8" ;;
    js) printf '%s' "application/javascript; charset=utf-8" ;;
    json) printf '%s' "application/json; charset=utf-8" ;;
    xml) printf '%s' "application/xml; charset=utf-8" ;;
    txt | log) printf '%s' "text/plain; charset=utf-8" ;;
    svg) printf '%s' "image/svg+xml" ;;
    jpg | jpeg) printf '%s' "image/jpeg" ;;
    png) printf '%s' "image/png" ;;
    gif) printf '%s' "image/gif" ;;
    webp) printf '%s' "image/webp" ;;
    ico) printf '%s' "image/x-icon" ;;
    *) printf '%s' "application/octet-stream" ;;
  esac
}

read_request_body() {
  if [ -z "${CONTENT_LENGTH:-}" ] || [ "$CONTENT_LENGTH" -le 0 ] 2>/dev/null; then
    return 0
  fi

  BODY_TMP="$(create_temp_file)" || send_text_response "500 Internal Server Error" "500 Internal Server Error: unable to create temp file"
  dd bs=1 count="$CONTENT_LENGTH" of="$BODY_TMP" 2>/dev/null || cat >"$BODY_TMP"
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\n/\\n/g; s/\r//g'
}

execute_virsh_and_return_json() {
  local action="$1"
  local vm_name="$2"

  case "$action" in
    start) virsh start "$vm_name" 2>&1 ;;
    shutdown) virsh shutdown "$vm_name" 2>&1 ;;
    destroy) virsh destroy "$vm_name" 2>&1 ;;
    reboot) virsh reboot "$vm_name" 2>&1 ;;
    reset) virsh reset "$vm_name" 2>&1 ;;
    *) return 1 ;;
  esac

  local rc=$?
  return $rc
}

handle_api_vms() {
  local vms_json=""
  local first=true
  local vm_names

  vms_json='['
  vm_names=$(virsh list --all --name 2>/dev/null)

  while IFS= read -r vm_name; do
    [ -z "$vm_name" ] && continue

    local info
    info=$(virsh dominfo "$vm_name" 2>/dev/null)

    local vm_state vm_uuid vm_maxmem vm_cpu vm_cputime
    vm_state=$(echo "$info" | sed -n 's/^State:[[:space:]]*//p')
    vm_uuid=$(echo "$info" | sed -n 's/^UUID:[[:space:]]*//p')
    vm_maxmem=$(echo "$info" | sed -n 's/^Max memory:[[:space:]]*//p' | awk '{print $1}')
    vm_cpu=$(echo "$info" | sed -n 's/^CPU(s):[[:space:]]*//p')
    vm_cputime=$(echo "$info" | sed -n 's/^CPU time:[[:space:]]*//p')

    local state_label
    case "$vm_state" in
      "running") state_label="running" ;;
      "shut off") state_label="shut_off" ;;
      "paused") state_label="paused" ;;
      *) state_label="unknown" ;;
    esac

    [ "$first" = true ] && first=false || vms_json+=','
    vms_json+="{"
    vms_json+="\"name\":\"$(json_escape "$vm_name")\","
    vms_json+="\"state\":\"$(json_escape "$state_label")\","
    vms_json+="\"uuid\":\"$(json_escape "$vm_uuid")\","
    vms_json+="\"vcpu\":${vm_cpu:-0},"
    vms_json+="\"memory\":${vm_maxmem:-0},"
    vms_json+="\"cpu_time\":\"$(json_escape "$vm_cputime")\""
    vms_json+="}"
  done <<EOF
$vm_names
EOF

  vms_json+=']'
  send_json_response "200 OK" "$vms_json"
}

handle_api_vm_action() {
  local vm_name="$1"
  local action="$2"

  if [ -z "$vm_name" ] || [ -z "$action" ]; then
    send_json_response "400 Bad Request" '{"error":"missing vm name or action"}'
  fi

  local output
  output=$(virsh "$action" "$vm_name" 2>&1)
  local rc=$?

  if [ $rc -eq 0 ]; then
    send_json_response "200 OK" "{\"success\":true,\"message\":\"$(json_escape "$output")\",\"vm\":\"$(json_escape "$vm_name")\",\"action\":\"$(json_escape "$action")\"}"
  else
    send_json_response "500 Internal Server Error" "{\"success\":false,\"error\":\"$(json_escape "$output")\",\"vm\":\"$(json_escape "$vm_name")\",\"action\":\"$(json_escape "$action")\"}"
  fi
}

handle_api_patch() {
  local target="/var/apps/trim.vm/target/static/index.html"
  local marker="<!-- fn-trim.vm-patch -->"
  local anchor="</title>"

  if [ "$REQUEST_METHOD" = "GET" ]; then
    local patched=false
    if [ -f "$target" ] && grep -qF "$marker" "$target" 2>/dev/null; then
      patched=true
    fi
    send_json_response "200 OK" "{\"patched\":$patched}"
  fi

  if [ "$REQUEST_METHOD" = "POST" ]; then
    read_request_body

    local enable
    enable="$("$PYTHON_BIN" -c "import sys,json; print('true' if json.load(open(sys.argv[1])).get('enabled') else 'false')" "$BODY_TMP" 2>/dev/null)"

    if [ "$enable" != "true" ] && [ "$enable" != "false" ]; then
      send_json_response "400 Bad Request" '{"error":"enabled must be true or false"}'
    fi

    if [ ! -f "$target" ]; then
      send_json_response "500 Internal Server Error" "{\"error\":\"target file not found: $(json_escape "$target")\"}"
    fi

    if [ "$enable" = "true" ]; then
      if grep -qF "$marker" "$target" 2>/dev/null; then
        send_json_response "200 OK" '{"success":true,"patched":true,"message":"already patched"}'
      fi

      local style_block='    <!-- fn-trim.vm-patch -->
    <style>
      @media all {
        #root button {
          display: inline-flex !important;
        }
        .cc--bottom-sheet .flex.flex-col.gap-2.px-4.pb-\[calc\(env\(safe-area-inset-bottom\)\+16px\)\] > .hidden {
          display: block !important;
        }
      }
    </style>'
      local tmpf
      tmpf="$(create_temp_file)" || send_json_response "500 Internal Server Error" '{"error":"cannot create temp file"}'
      printf '%s\n' "$style_block" >"$tmpf"
      sed -i "\|${anchor}|r ${tmpf}" "$target"
      local rc=$?
      rm -f "$tmpf"
      if [ $rc -eq 0 ]; then
        send_json_response "200 OK" '{"success":true,"patched":true,"message":"patch applied"}'
      else
        send_json_response "500 Internal Server Error" '{"error":"failed to apply patch"}'
      fi
    else
      if grep -qF "$marker" "$target" 2>/dev/null; then
        sed -i "\|${marker}|,\|</style>|d" "$target"
        send_json_response "200 OK" '{"success":true,"patched":false,"message":"patch removed"}'
      else
        send_json_response "200 OK" '{"success":true,"patched":false,"message":"not patched"}'
      fi
    fi
  fi
}

handle_api() {
  local rel="$1"
  rel="${rel#/api}"
  rel="${rel#/}"

  case "$rel" in
    vms)
      handle_api_vms
      ;;
    patch)
      handle_api_patch
      ;;
    vm/*/start|vm/*/shutdown|vm/*/destroy|vm/*/reboot|vm/*/reset)
      local vm_name="${rel#vm/}"
      local action="${vm_name##*/}"
      vm_name="${vm_name%/*}"
      handle_api_vm_action "$vm_name" "$action"
      ;;
    vm/*)
      local vm_name="${rel#vm/}"
      local info
      info=$(virsh dominfo "$vm_name" 2>/dev/null)
      if [ -z "$info" ]; then
        send_json_response "404 Not Found" "{\"error\":\"vm not found: $(json_escape "$vm_name")\"}"
      fi
      local json="{"
      json+="\"name\":\"$(json_escape "$vm_name")\","
      while IFS=':' read -r key value; do
        key=$(printf '%s' "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_]/_/g')
        value=$(printf '%s' "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [ -z "$key" ] && continue
        json+="\"$(json_escape "$key")\":\"$(json_escape "$value")\","
      done <<EOF
$info
EOF
      json="${json%,}"
      json+="}"
      send_json_response "200 OK" "$json"
      ;;
    *)
      send_json_response "404 Not Found" "{\"error\":\"unknown api endpoint: $(json_escape "$rel")\"}"
      ;;
  esac
}

serve_static_file() {
  local target_file mime mtime size last_mod

  case "$REQUEST_METHOD" in
    GET | HEAD)
      ;;
    *)
      send_text_response "405 Method Not Allowed" "405 Method Not Allowed"
      ;;
  esac

  if is_path_traversal "${REL_PATH#/}"; then
    send_text_response "400 Bad Request" "Bad Request: Path traversal detected"
  fi

  target_file="${BASE_PATH}${REL_PATH}"
  if [ ! -f "$target_file" ]; then
    send_text_response "404 Not Found" "404 Not Found: ${REL_PATH}"
  fi

  mime="$(detect_mime "$target_file")"
  mtime=0
  size=0
  if stat -c %Y "$target_file" >/dev/null 2>&1; then
    mtime="$(stat -c %Y "$target_file" 2>/dev/null || echo 0)"
    size="$(stat -c %s "$target_file" 2>/dev/null || echo 0)"
  fi

  print_header "Content-Type: $mime"
  print_header "Content-Length: $size"
  print_header ""

  if [ "$REQUEST_METHOD" != "HEAD" ]; then
    cat "$target_file"
  fi
}

trap cleanup EXIT

REL_PATH="$(resolve_rel_path)"

case "$REL_PATH" in
  /api | /api/*)
    handle_api "$REL_PATH"
    ;;
  *)
    serve_static_file
    ;;
esac
