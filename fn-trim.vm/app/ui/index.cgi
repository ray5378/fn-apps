#!/bin/bash

BASE_PATH="${TRIM_APPDEST:-/var/apps/fn-trim.vm/target}/www"
REQUEST_METHOD="$(printf '%s' "${REQUEST_METHOD:-GET}" | tr '[:lower:]' '[:upper:]')"

RESP_STATUS="200 OK"
RESP_CTYPE="text/html; charset=utf-8"
RESP_BODY=""

read_body() {
  if [ "$REQUEST_METHOD" = "POST" ]; then
    cat
  fi
}

send_response() {
  printf 'Status: %s\r\n' "$RESP_STATUS"
  printf 'Content-Type: %s\r\n' "$RESP_CTYPE"
  printf '\r\n'
  printf '%s' "$RESP_BODY"
  exit 0
}

send_json() {
  local status="$1"
  local body="$2"
  RESP_STATUS="$status"
  RESP_CTYPE="application/json; charset=utf-8"
  RESP_BODY="$body"
  send_response
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\n/\\n/g; s/\r//g'
}

list_vms() {
  local vms_json=""
  local first=true
  vms_json+='['

  while IFS='|' read -r name state domain_id uuid used_mem max_mem cpu_time cpu_count; do
    if [ -z "$name" ]; then
      continue
    fi
    if [ "$first" = true ]; then
      first=false
    else
      vms_json+=','
    fi

    local state_label
    case "$state" in
      "running") state_label="running" ;;
      "shut off"|"shut") state_label="shut_off" ;;
      "paused") state_label="paused" ;;
      *) state_label="unknown" ;;
    esac

    vms_json+="{"
    vms_json+="\"name\":\"$(json_escape "$name")\","
    vms_json+="\"state\":\"$(json_escape "$state_label")\","
    vms_json+="\"uuid\":\"$(json_escape "$uuid")\","
    vms_json+="\"vcpu\":$(json_escape "${cpu_count:-0}"),"
    vms_json+="\"memory\":$(json_escape "${max_mem:-0}"),"
    vms_json+="\"cpu_time\":\"$(json_escape "$cpu_time")\""
    vms_json+="}"
  done <<EOF
$(virsh list --all --name 2>/dev/null | while read -r vm_name; do
  [ -z "$vm_name" ] && continue
  dom_info=$(virsh dominfo "$vm_name" 2>/dev/null)
  vm_state=$(echo "$dom_info" | grep '^State:' | sed 's/^State:[[:space:]]*//')
  vm_uuid=$(echo "$dom_info" | grep '^UUID:' | sed 's/^UUID:[[:space:]]*//')
  vm_maxmem=$(echo "$dom_info" | grep '^Max memory:' | sed 's/^Max memory:[[:space:]]*//' | awk '{print $1}')
  vm_cpu=$(echo "$dom_info" | grep '^CPU(s):' | sed 's/^CPU(s):[[:space:]]*//')
  vm_cputime=$(echo "$dom_info" | grep '^CPU time:' | sed 's/^CPU time:[[:space:]]*//')
  if virsh domid "$vm_name" >/dev/null 2>&1; then
    vm_domid=$(virsh domid "$vm_name" 2>/dev/null)
  else
    vm_domid="-"
  fi
  echo "${vm_name}|${vm_state}|${vm_domid}|${vm_uuid}|||${vm_cputime}|${vm_cpu}"
done)
EOF
  vms_json+=']'
  send_json "200 OK" "$vms_json"
}

vm_action() {
  local vm_name="$1"
  local action="$2"

  if [ -z "$vm_name" ]; then
    send_json "400 Bad Request" '{"error":"missing vm name"}'
    return
  fi

  local virsh_cmd=""
  case "$action" in
    start) virsh_cmd="start" ;;
    shutdown) virsh_cmd="shutdown" ;;
    destroy) virsh_cmd="destroy" ;;
    reboot) virsh_cmd="reboot" ;;
    reset) virsh_cmd="reset" ;;
    *)
      send_json "400 Bad Request" "{\"error\":\"unknown action: $(json_escape "$action")\"}"
      return
      ;;
  esac

  local output
  output=$(virsh "$virsh_cmd" "$vm_name" 2>&1)
  local rc=$?

  if [ $rc -eq 0 ]; then
    send_json "200 OK" "{\"success\":true,\"message\":\"$(json_escape "$output")\",\"vm\":\"$(json_escape "$vm_name")\",\"action\":\"$(json_escape "$action")\"}"
  else
    send_json "500 Internal Server Error" "{\"success\":false,\"error\":\"$(json_escape "$output")\",\"vm\":\"$(json_escape "$vm_name")\",\"action\":\"$(json_escape "$action")\"}"
  fi
}

get_patch_status() {
  local target="/var/apps/trim.vm/target/static/index.html"
  local marker="<!-- fn-trim.vm-patch -->"
  local patched=false

  if [ -f "$target" ] && grep -qF "$marker" "$target" 2>/dev/null; then
    patched=true
  fi

  send_json "200 OK" "{\"patched\":$patched}"
}

toggle_patch() {
  local target="/var/apps/trim.vm/target/static/index.html"
  local anchor="</title>"
  local marker="<!-- fn-trim.vm-patch -->"
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

  local body
  body=$(cat)
  local enable
  enable=$(printf '%s' "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('enabled',''))" 2>/dev/null)

  if [ "$enable" != "true" ] && [ "$enable" != "false" ]; then
    send_json "400 Bad Request" '{"error":"enabled must be true or false"}'
    return
  fi

  if [ ! -f "$target" ]; then
    send_json "500 Internal Server Error" "{\"error\":\"target file not found: $(json_escape "$target")\"}"
    return
  fi

  if [ "$enable" = "true" ]; then
    if grep -qF "$marker" "$target" 2>/dev/null; then
      send_json "200 OK" '{"success":true,"patched":true,"message":"already patched"}'
    else
      local tmpf
      tmpf=$(mktemp)
      printf '%s\n' "$style_block" >"$tmpf"
      sed -i "\|${anchor}|r ${tmpf}" "$target"
      local rc=$?
      rm -f "$tmpf"
      if [ $rc -eq 0 ]; then
        send_json "200 OK" '{"success":true,"patched":true,"message":"patch applied"}'
      else
        send_json "500 Internal Server Error" '{"error":"failed to apply patch"}'
      fi
    fi
  else
    if grep -qF "$marker" "$target" 2>/dev/null; then
      sed -i "\|${marker}|,\|</style>|d" "$target"
      send_json "200 OK" '{"success":true,"patched":false,"message":"patch removed"}'
    else
      send_json "200 OK" '{"success":true,"patched":false,"message":"not patched"}'
    fi
  fi
}

get_vm_detail() {
  local vm_name="$1"
  if [ -z "$vm_name" ]; then
    send_json "400 Bad Request" '{"error":"missing vm name"}'
    return
  fi

  local info
  info=$(virsh dominfo "$vm_name" 2>/dev/null)
  if [ -z "$info" ]; then
    send_json "404 Not Found" "{\"error\":\"vm not found: $(json_escape "$vm_name")\"}"
    return
  fi

  local json="{"
  json+="\"name\":\"$(json_escape "$vm_name")\","

  while IFS=':' read -r key value; do
    key=$(printf '%s' "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_]/_/g')
    value=$(printf '%s' "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    [ -z "$key" ] && continue
    json+="\"$(json_escape "$key")\":\"$(json_escape "$value")\","
  done <<EOF
$(echo "$info")
EOF
  json="${json%,}"
  json+="}"
  send_json "200 OK" "$json"
}

serve_static() {
  local path="$1"
  path="${path#/}"
  path="${path#index.cgi/}"

  if [ -z "$path" ]; then
    path="index.html"
  fi

  local file="${BASE_PATH}/${path}"
  file="$(realpath "$file" 2>/dev/null)"

  case "$file" in
    "${BASE_PATH}"/*)
      ;;
    *)
      send_json "403 Forbidden" '{"error":"forbidden"}'
      return
      ;;
  esac

  if [ ! -f "$file" ]; then
    send_json "404 Not Found" "{\"error\":\"file not found: $(json_escape "$path")\"}"
    return
  fi

  case "$file" in
    *.html) RESP_CTYPE="text/html; charset=utf-8" ;;
    *.css) RESP_CTYPE="text/css; charset=utf-8" ;;
    *.js) RESP_CTYPE="application/javascript; charset=utf-8" ;;
    *.png) RESP_CTYPE="image/png" ;;
    *.jpg|*.jpeg) RESP_CTYPE="image/jpeg" ;;
    *.svg) RESP_CTYPE="image/svg+xml" ;;
    *.ico) RESP_CTYPE="image/x-icon" ;;
    *.json) RESP_CTYPE="application/json; charset=utf-8" ;;
    *) RESP_CTYPE="application/octet-stream" ;;
  esac

  RESP_STATUS="200 OK"
  RESP_BODY=""
  printf 'Status: %s\r\n' "$RESP_STATUS"
  printf 'Content-Type: %s\r\n' "$RESP_CTYPE"
  printf '\r\n'
  cat "$file"
  exit 0
}

REQUEST_PATH="${REQUEST_URI:-}"
CGI_PATH="${SCRIPT_NAME:-/cgi/ThirdParty/fn-trim.vm/index.cgi}"

REL_PATH="${REQUEST_PATH#${CGI_PATH}}"
REL_PATH="${REL_PATH#/}"
REL_PATH="${REL_PATH%%\?*}"
REL_PATH="${REL_PATH%%#*}"

if [ -z "$REL_PATH" ]; then
  serve_static "index.html"
fi

case "$REL_PATH" in
  api/vms)
    list_vms
    ;;
  api/vm/*/start|api/vm/*/shutdown|api/vm/*/destroy|api/vm/*/reboot|api/vm/*/reset)
    local vm_name="${REL_PATH#api/vm/}"
    vm_name="${vm_name%/*}"
    local action="${REL_PATH##*/}"
    [ "$REQUEST_METHOD" = "POST" ] && read_body
    vm_action "$vm_name" "$action"
    ;;
  api/vm/*)
    local vm_name="${REL_PATH#api/vm/}"
    get_vm_detail "$vm_name"
    ;;
  api/patch)
    if [ "$REQUEST_METHOD" = "POST" ]; then
      read_body
      toggle_patch
    else
      get_patch_status
    fi
    ;;
  *)
    serve_static "$REL_PATH"
    ;;
esac
