#!/usr/bin/env bash
#
# llama-installer.sh — install llama.cpp (from source) + the Hugging Face CLI,
# then pick a GGUF model with a live type-ahead search bar and (optionally)
# start chatting with it right away.
#
# Usage:            bash llama-installer.sh
# Re-run anytime:   already-installed pieces are detected and skipped.
#
set -euo pipefail

# ────────────────────────── appearance ──────────────────────────────────────
# Hidden __ verbs are invoked by fzf with stdout piped, but fzf renders ANSI
# (--ansi), so force colors on for them.
if [[ -t 1 || "${1:-}" == __* ]]; then
  C_RESET=$'\e[0m'   C_BOLD=$'\e[1m'    C_DIM=$'\e[2m'
  C_CYAN=$'\e[36m'   C_GREEN=$'\e[32m'  C_RED=$'\e[31m'
  C_YELLOW=$'\e[33m' C_MAGENTA=$'\e[35m'
else
  C_RESET='' C_BOLD='' C_DIM='' C_CYAN='' C_GREEN='' C_RED='' C_YELLOW='' C_MAGENTA=''
fi

banner() {
  printf '%s' "$C_CYAN"
  cat <<'EOF'

   ██╗     ██╗      █████╗ ███╗   ███╗ █████╗    ██████╗██████╗ ██████╗
   ██║     ██║     ██╔══██╗████╗ ████║██╔══██╗  ██╔════╝██╔══██╗██╔══██╗
   ██║     ██║     ███████║██╔████╔██║███████║  ██║     ██████╔╝██████╔╝
   ██║     ██║     ██╔══██║██║╚██╔╝██║██╔══██║  ██║     ██╔═══╝ ██╔═══╝
   ███████╗███████╗██║  ██║██║ ╚═╝ ██║██║  ██║██╗╚██████╗██║     ██║
   ╚══════╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝╚═╝ ╚═════╝╚═╝     ╚═╝
EOF
  printf '%s' "$C_RESET"
  printf '   %sllama.cpp + Hugging Face CLI installer%s\n' "$C_BOLD" "$C_RESET"
  printf '   %sbuilds from source · live model search · zero clutter%s\n\n' "$C_DIM" "$C_RESET"
}

section() { printf '\n%s[%s/5]%s %s%s%s\n' "$C_MAGENTA" "$1" "$C_RESET" "$C_BOLD" "$2" "$C_RESET"; }
info()    { printf '  %s·%s %s\n' "$C_DIM" "$C_RESET" "$1"; }
ok()      { printf '  %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
warn()    { printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$1"; }
die()     { printf '  %s✗ %s%s\n' "$C_RED" "$1" "$C_RESET" >&2; exit 1; }

LOG="$(mktemp -t llama-installer.XXXXXX.log)"
trap 'printf "\n"; die "interrupted (log: $LOG)"' INT

# Run a slow command with a spinner; on failure show the tail of its log.
run_step() {
  local msg="$1"; shift
  if [[ ! -t 1 ]]; then
    printf '  … %s\n' "$msg"
    "$@" >>"$LOG" 2>&1 || { tail -n 25 "$LOG" >&2; die "$msg failed (full log: $LOG)"; }
    ok "$msg"
    return
  fi
  "$@" >>"$LOG" 2>&1 &
  local pid=$! frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' i=0
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r  %s%s%s %s ' "$C_CYAN" "${frames:i++%10:1}" "$C_RESET" "$msg"
    sleep 0.1
  done
  if wait "$pid"; then
    printf '\r  %s✓%s %s  \n' "$C_GREEN" "$C_RESET" "$msg"
  else
    printf '\r  %s✗ %s%s  \n' "$C_RED" "$msg" "$C_RESET"
    printf '%s── last lines of log ──%s\n' "$C_DIM" "$C_RESET" >&2
    tail -n 25 "$LOG" >&2
    die "step failed — full log: $LOG"
  fi
}

# ────────────────────────── helpers ─────────────────────────────────────────
human_n() {  # 1234567 -> 1.2M
  awk -v n="${1:-0}" 'BEGIN{
    if (n>=1e9)      printf "%.1fB", n/1e9
    else if (n>=1e6) printf "%.1fM", n/1e6
    else if (n>=1e3) printf "%.1fk", n/1e3
    else             printf "%d",   n }'
}

human_size() {  # bytes -> 2.0G
  awk -v n="${1:-0}" 'BEGIN{
    if (n>=2^40)      printf "%.1fT", n/2^40
    else if (n>=2^30) printf "%.1fG", n/2^30
    else if (n>=2^20) printf "%.0fM", n/2^20
    else if (n>=2^10) printf "%.0fK", n/2^10
    else              printf "%dB",  n }'
}

urlencode() { jq -rn --arg q "$1" '$q|@uri'; }

# ────────────────────────── HF API subcommands (used by fzf) ────────────────
# `fzf` re-invokes this script with hidden verbs to fill the list and preview.

hf_search() {  # $* = query → "repo_id<TAB>⬇ dl  ♥ likes" per line
  local q="$*" url
  url="https://huggingface.co/api/models?filter=gguf&sort=downloads&direction=-1&limit=30"
  [[ -n "$q" ]] && url+="&search=$(urlencode "$q")"
  curl -sf --max-time 10 "$url" \
    | jq -r '.[] | "\(.id)\t\(.downloads // 0)\t\(.likes // 0)"' \
    | while IFS=$'\t' read -r id dl likes; do
        printf '%s\t%s⬇ %-7s ♥ %s%s\n' \
          "$id" "$C_DIM" "$(human_n "$dl")" "$(human_n "$likes")" "$C_RESET"
      done
}

# Collapse sharded models (foo-00001-of-00003.gguf …) into one logical entry.
# stdin: repo JSON → "first_file<TAB>n_parts<TAB>total_bytes" per line, sorted.
gguf_groups() {
  jq -r '.siblings[]? | select(.rfilename|test("\\.gguf$")) | "\(.rfilename)\t\(.size // 0)"' \
    | awk -F'\t' '{
        f = $1; base = f
        if (match(f, /-[0-9]{5}-of-[0-9]{5}\.gguf$/)) base = substr(f, 1, RSTART - 1)
        cnt[base]++; tot[base] += $2
        if (!(base in first) || f < first[base]) first[base] = f   # keep part 00001
      }
      END { for (b in cnt) printf "%s\t%d\t%.0f\n", first[b], cnt[b], tot[b] }' \
    | sort
}

hf_preview() {  # $1 = repo_id → summary + GGUF file list with sizes
  local repo="$1" json
  json=$(curl -sf --max-time 8 "https://huggingface.co/api/models/${repo}?blobs=true") \
    || { echo "  (couldn't fetch model info)"; return 0; }
  local dl likes mod
  dl=$(jq -r '.downloads // 0'                    <<<"$json")
  likes=$(jq -r '.likes // 0'                     <<<"$json")
  mod=$(jq -r '(.lastModified // "")[:10]'        <<<"$json")
  printf '%s%s%s\n' "$C_BOLD" "$repo" "$C_RESET"
  printf '⬇ %s downloads   ♥ %s likes   updated %s\n\n' \
    "$(human_n "$dl")" "$(human_n "$likes")" "${mod:-?}"
  printf '%sGGUF files:%s\n' "$C_BOLD" "$C_RESET"
  gguf_groups <<<"$json" \
    | while IFS=$'\t' read -r f n s; do
        note=""; [[ "$n" -gt 1 ]] && note="  ($n parts)"
        printf ' • %-52s %8s%s\n' "$f" "$(human_size "$s")" "$note"
      done
}

hf_files() {  # $1 = repo_id → quant picker rows; sharded models appear once,
              # anchored on part 00001 with their total size and part count
  curl -sf --max-time 10 "https://huggingface.co/api/models/${1}?blobs=true" \
    | gguf_groups \
    | while IFS=$'\t' read -r f n s; do
        note=""; [[ "$n" -gt 1 ]] && note="  ($n parts)"
        printf '%s\t%s%8s%s%s\n' "$f" "$C_DIM" "$(human_size "$s")" "$note" "$C_RESET"
      done
}

# ────────────────────────── platform detection ──────────────────────────────
detect_platform() {
  OS="$(uname -s)"
  PM=""
  for pm in apt-get dnf pacman zypper brew; do
    command -v "$pm" >/dev/null 2>&1 && { PM="$pm"; break; }
  done
  [[ -n "$PM" ]] || die "no supported package manager found (apt/dnf/pacman/zypper/brew)"

  if [[ "$(id -u)" == 0 ]]; then
    SUDO=""; PREFIX="/usr/local"
  else
    command -v sudo >/dev/null 2>&1 || die "not root and sudo is unavailable"
    SUDO="sudo"; PREFIX="$HOME/.local"
  fi
  BIN_DIR="$PREFIX/bin"
  SRC_DIR="${LLAMA_SRC_DIR:-$HOME/.local/share/llama.cpp}"
  MODEL_DIR="${LLAMA_MODEL_DIR:-$HOME/models}"
  JOBS="$( (command -v nproc >/dev/null && nproc) || sysctl -n hw.ncpu 2>/dev/null || echo 4 )"

  # Tools we install ourselves (cmake/fzf fallbacks, pipx apps) must win over
  # stale system versions for the rest of this run.
  export PATH="$BIN_DIR:$HOME/.local/bin:$PATH"

  info "platform: $OS · package manager: $PM · install prefix: $PREFIX"
}

pm_refresh() {
  case "$PM" in
    apt-get) $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq ;;
    pacman)  $SUDO pacman -Sy --noconfirm ;;
    *)       : ;;
  esac
}

pm_install() {
  case "$PM" in
    apt-get) $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    dnf)     $SUDO dnf install -y "$@" ;;
    pacman)  $SUDO pacman -S --noconfirm --needed "$@" ;;
    zypper)  $SUDO zypper --non-interactive install "$@" ;;
    brew)    brew install "$@" ;;
  esac
}

ver_ge() {  # ver_ge HAVE NEED → success if HAVE ≥ NEED (numeric dotted versions)
  awk -v a="${1:-0}" -v b="${2:-0}" 'BEGIN{
    split(a,x,"."); split(b,y,".")
    for (i = 1; i <= 3; i++) { if (x[i]+0 > y[i]+0) exit 0; if (x[i]+0 < y[i]+0) exit 1 }
    exit 0 }'
}

# Old distros (e.g. Ubuntu 20.04) ship cmake < 3.18 / fzf < 0.23, which are too
# old for llama.cpp / the live search bar. Fall back to official binaries.
ensure_cmake() {
  local need=3.18 have=""
  command -v cmake >/dev/null 2>&1 && have="$(cmake --version | awk 'NR==1{print $3}')"
  if [[ -n "$have" ]] && ver_ge "$have" "$need"; then ok "cmake $have"; return; fi
  local ver=3.31.8 arch
  case "$OS/$(uname -m)" in
    Linux/x86_64)          arch=x86_64  ;;
    Linux/aarch64)         arch=aarch64 ;;
    *) die "cmake ${have:-missing} is too old (need ≥ $need) — please install a newer cmake and re-run" ;;
  esac
  warn "system cmake ${have:-not found} (need ≥ $need) — installing CMake $ver → $PREFIX"
  run_step "download CMake $ver" bash -c \
    "mkdir -p '$PREFIX' && curl -fsSL 'https://github.com/Kitware/CMake/releases/download/v${ver}/cmake-${ver}-linux-${arch}.tar.gz' | tar xz -C '$PREFIX' --strip-components=1"
  hash -r
}

ensure_fzf() {
  local need=0.23 have=""
  command -v fzf >/dev/null 2>&1 && have="$(fzf --version | awk '{print $1}')"
  if [[ -n "$have" ]] && ver_ge "$have" "$need"; then ok "fzf $have"; return; fi
  local ver=0.62.0 arch
  case "$OS/$(uname -m)" in
    Linux/x86_64)  arch=amd64 ;;
    Linux/aarch64) arch=arm64 ;;
    *) die "fzf ${have:-missing} is too old (need ≥ $need for live search) — please install a newer fzf and re-run" ;;
  esac
  warn "system fzf ${have:-not found} (need ≥ $need for live search) — installing fzf $ver → $BIN_DIR"
  run_step "download fzf $ver" bash -c \
    "mkdir -p '$BIN_DIR' && curl -fsSL 'https://github.com/junegunn/fzf/releases/download/v${ver}/fzf-${ver}-linux_${arch}.tar.gz' | tar xz -C '$BIN_DIR' fzf"
  hash -r
}

# ────────────────────────── step 1: prerequisites ────────────────────────────
install_deps() {
  section 1 "Prerequisites"
  # `core` must succeed; `opt` may be missing on old distros (fallbacks exist).
  local core=() opt=()
  case "$PM" in
    apt-get) core=(build-essential cmake git curl jq ca-certificates libcurl4-openssl-dev python3 python3-pip python3-venv)
             opt=(pipx fzf) ;;
    dnf)     core=(gcc gcc-c++ make cmake git curl jq libcurl-devel python3 python3-pip)
             opt=(pipx fzf) ;;
    pacman)  core=(base-devel cmake git curl jq python python-pip)
             opt=(python-pipx fzf) ;;
    zypper)  core=(gcc gcc-c++ make cmake git curl jq libcurl-devel python3 python3-pip)
             opt=(python3-pipx fzf) ;;
    brew)    core=(cmake git curl jq)
             opt=(pipx fzf) ;;
  esac
  run_step "refresh package index (${PM})" pm_refresh
  run_step "install core build tools (${PM})" pm_install "${core[@]}"
  local p
  for p in "${opt[@]}"; do
    if pm_install "$p" >>"$LOG" 2>&1; then
      ok "$p"
    else
      warn "$p not available via ${PM} — a fallback will be used"
    fi
  done
  ensure_cmake
  ensure_fzf
}

# ────────────────────────── step 2: llama.cpp ────────────────────────────────
install_llama() {
  section 2 "llama.cpp (built from source)"

  if [[ -z "${LLAMA_FORCE_BUILD:-}" ]] && command -v llama-cli >/dev/null 2>&1; then
    ok "llama-cli already on PATH ($(command -v llama-cli)) — skipping build (set LLAMA_FORCE_BUILD=1 to rebuild)"
    return
  fi

  if [[ -d "$SRC_DIR/.git" ]]; then
    run_step "update source in $SRC_DIR" git -C "$SRC_DIR" pull --ff-only
  else
    run_step "clone ggml-org/llama.cpp → $SRC_DIR" \
      git clone --depth 1 https://github.com/ggml-org/llama.cpp "$SRC_DIR"
  fi

  # Static libs → self-contained binaries that run from any prefix without
  # ldconfig / LD_LIBRARY_PATH fiddling.
  local cmake_flags=(-DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=ON -DBUILD_SHARED_LIBS=OFF)
  if [[ "$OS" == "Darwin" ]]; then
    info "acceleration: Metal (automatic on macOS)"
  elif command -v nvcc >/dev/null 2>&1; then
    cmake_flags+=(-DGGML_CUDA=ON)
    info "acceleration: CUDA (nvcc found)"
  else
    info "acceleration: CPU with native optimizations (no CUDA toolkit found)"
  fi

  run_step "configure (cmake)" \
    cmake -S "$SRC_DIR" -B "$SRC_DIR/build" "${cmake_flags[@]}"
  run_step "compile with $JOBS jobs (this is the long one — grab a coffee)" \
    cmake --build "$SRC_DIR/build" --config Release -j "$JOBS"
  run_step "install binaries → $BIN_DIR" \
    cmake --install "$SRC_DIR/build" --prefix "$PREFIX"

  if ! command -v llama-cli >/dev/null 2>&1 && [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR is not in your PATH — add this to your shell profile:"
    printf '      %sexport PATH="%s:$PATH"%s\n' "$C_BOLD" "$BIN_DIR" "$C_RESET"
    export PATH="$BIN_DIR:$PATH"
  fi
  ok "llama.cpp installed"
}

# ────────────────────────── step 3: Hugging Face CLI ─────────────────────────
install_hf() {
  section 3 "Hugging Face CLI"
  HF_BIN="$(command -v hf || command -v huggingface-cli || true)"
  if [[ -n "$HF_BIN" ]]; then
    ok "already installed ($HF_BIN)"
    return
  fi
  if command -v pipx >/dev/null 2>&1; then
    run_step "pipx install huggingface_hub[cli]" pipx install 'huggingface_hub[cli]'
    command -v pipx >/dev/null && pipx ensurepath >>"$LOG" 2>&1 || true
  else
    # --break-system-packages (PEP 668) doesn't exist on older pip (e.g. 20.04's)
    local pipflags=(--user)
    python3 -m pip install --help 2>/dev/null | grep -q -- --break-system-packages \
      && pipflags+=(--break-system-packages)
    run_step "pip install --user huggingface_hub[cli]" \
      python3 -m pip install "${pipflags[@]}" 'huggingface_hub[cli]'
  fi
  export PATH="$HOME/.local/bin:$PATH"
  HF_BIN="$(command -v hf || command -v huggingface-cli || true)"
  [[ -n "$HF_BIN" ]] || die "huggingface CLI installed but not found on PATH — open a new shell and re-run"
  ok "Hugging Face CLI ready ($HF_BIN)"
}

# ────────────────────────── step 4: model search bar ─────────────────────────
pick_model() {
  section 4 "Choose a model"
  [[ -t 0 && -t 1 ]] || die "model search needs an interactive terminal"
  info "type to search Hugging Face — results fill in live · Enter selects · Esc quits"

  local fzf_colors='bg+:-1,fg+:cyan:bold,pointer:cyan,prompt:cyan,border:8,header:8'

  REPO="$(
    FZF_DEFAULT_COMMAND="${SELF@Q} __search ''" \
    fzf --ansi --disabled \
        --query '' \
        --prompt '  ' \
        --header '── Search Hugging Face models (GGUF) ──' \
        --delimiter '\t' --with-nth 1,2 --nth 1 \
        --bind "change:reload:sleep 0.25; ${SELF@Q} __search {q} || true" \
        --preview "${SELF@Q} __preview {1}" \
        --preview-window 'down,45%,border-top,wrap' \
        --height 90% --layout reverse --border rounded \
        --color "$fzf_colors" \
      | cut -f1
  )" || true
  [[ -n "${REPO:-}" ]] || die "no model selected"
  ok "model: $C_BOLD$REPO$C_RESET"

  GGUF="$(
    "$SELF" __files "$REPO" \
      | fzf --ansi \
            --prompt '  ' \
            --header "── Pick a quantization of $REPO ──" \
            --delimiter '\t' \
            --height 60% --layout reverse --border rounded \
            --color "$fzf_colors" \
        | cut -f1
  )" || true
  [[ -n "${GGUF:-}" ]] || die "no file selected"
  ok "file:  $C_BOLD$GGUF$C_RESET"
}

# ────────────────────────── step 5: download & run ───────────────────────────
download_and_run() {
  section 5 "Download & run"
  local dest="$MODEL_DIR/${REPO//\//_}"
  mkdir -p "$dest"

  # Sharded models ship as name-00001-of-00003.gguf — grab every shard.
  local include="$GGUF"
  if [[ "$GGUF" =~ ^(.*)-[0-9]{5}-of-[0-9]{5}\.gguf$ ]]; then
    include="${BASH_REMATCH[1]}-*.gguf"
    info "sharded model detected — downloading all parts ($include)"
  fi

  run_step "download $GGUF → $dest" \
    "$HF_BIN" download "$REPO" --include "$include" --local-dir "$dest"

  local model_path="$dest/$GGUF"
  [[ -f "$model_path" ]] || model_path="$(find "$dest" -name '*.gguf' | sort | head -n1)"

  printf '\n  %sAll set!%s Run it any time with:\n\n' "$C_GREEN$C_BOLD" "$C_RESET"
  printf '    %sllama-cli    -m %q%s   %s# terminal chat%s\n'   "$C_BOLD" "$model_path" "$C_RESET" "$C_DIM" "$C_RESET"
  printf '    %sllama-server -m %q%s   %s# OpenAI-compatible API on :8080%s\n\n' \
    "$C_BOLD" "$model_path" "$C_RESET" "$C_DIM" "$C_RESET"

  local ans
  read -rp "  Launch chat with it now? [Y/n] " ans
  if [[ ! "$ans" =~ ^[Nn] ]]; then
    printf '\n'
    exec llama-cli -m "$model_path"
  fi
}

# ────────────────────────── entry point ──────────────────────────────────────
SELF="$(realpath "${BASH_SOURCE[0]}")"

case "${1:-}" in
  __search)  shift; hf_search "$@";        exit 0 ;;
  __preview) shift; hf_preview "$1";       exit 0 ;;
  __files)   shift; hf_files "$1";         exit 0 ;;
  -h|--help)
    sed -n '2,9p' "$SELF" | sed 's/^# \{0,1\}//'
    exit 0 ;;
esac

banner
detect_platform
install_deps
install_llama
install_hf
pick_model
download_and_run
