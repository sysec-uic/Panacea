# ARVO Bug 42470017 — Investigation & Fix

**Project:** curl · **Fuzz target:** `curl_fuzzer_http` · **Engine/Sanitizer:** libFuzzer / UBSan
**Crash type:** Null-dereference READ (SEGV on `0x0`) · **Severity:** Medium
**Image:** `n132/arvo:42470017-vul` · **Vulnerable HEAD:** `396fc08 http2: remove four unused nghttp2 callbacks`

> Constraint for this exercise: the `-vul` image is treated as the latest version that exists.
> The upstream fix commit / patch URL were **not** consulted; the fix below was derived
> independently from the crash and the source.

---

## 1. Reproduce

Inside the container, `arvo` runs `/out/curl_fuzzer_http /tmp/poc` (UBSan build):

```
UndefinedBehaviorSanitizer:DEADLYSIGNAL
==28==ERROR: UndefinedBehaviorSanitizer: SEGV on unknown address 0x000000000000
==28==Hint: pc points to the zero page.
==28==The signal is caused by a READ memory access.
```

**Key reading of the symptom:** `pc points to the zero page` means the *program counter* itself
is 0 — the CPU jumped to address 0 and the "READ" is the failed instruction fetch. That is the
signature of a **call through a NULL function pointer**, not an ordinary null data read.

## 2. Root-cause investigation (gdb)

A backtrace pinpoints the call site (frame #0 is the NULL target, frame #1 is the caller):

```
#0  0x0000000000000000 in ?? ()
#1  send_callback (... data="PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n", length=24, userp=...) at http2.c:373
#2  nghttp2_session_send () at nghttp2_session.c:3210
#3  Curl_http2_done (conn=..., premature=true) at http2.c:1109
#4  Curl_http_done (..., status=CURLE_WEIRD_SERVER_REPLY, premature=true) at http.c:1568
#5  multi_done (..., premature=true) at multi.c:564
#6  multi_runsingle (...) at multi.c:1960
#7  curl_multi_perform (...) at multi.c:2171
#8  fuzz_handle_transfer(...) at curl_fuzzer.cc:382
```

The crashing line, `lib/http2.c:373`:

```c
written = ((Curl_send*)c->send_underlying)(conn, FIRSTSOCKET, data, length, &result);
```

So `c->send_underlying` is the NULL function pointer being invoked.

### Where the pointer lives and how it's set

```
lib/http2.c:211    if(httpc->recv_underlying)             // recv path GUARDS the pointer
lib/http2.c:213      nread = ((Curl_recv *)httpc->recv_underlying)(...)
lib/http2.c:373    written = ((Curl_send*)c->send_underlying)(...)   // send path does NOT guard
lib/http2.c:2110   httpc->recv_underlying = conn->recv[FIRSTSOCKET]; // both set together,
lib/http2.c:2111   httpc->send_underlying = conn->send[FIRSTSOCKET]; //   only in Curl_http2_switched()
```

The recv path at line 211 already carries the explanatory comment:

```c
if(httpc->recv_underlying)
  /* if called "too early", this pointer isn't setup yet! */
```

Both `recv_underlying` and `send_underlying` are initialized **only** in `Curl_http2_switched()`.
The backtrace shows we arrive at `send_callback` via **premature teardown**
(`Curl_http2_done(premature=true)` → `nghttp2_session_send`, which flushes the queued
`PRI * HTTP/2.0...` connection-preface frame). The nghttp2 session and its `send_callback`
were registered, but the connection never finished switching to HTTP/2 — so `send_underlying`
is still NULL when nghttp2 tries to flush.

The **recv path guards against exactly this; the send path does not.** That asymmetry is the bug.

### Hypothesis confirmed in the debugger

```
(gdb) print c->send_underlying   ->  $2 = (Curl_send *) 0x0
(gdb) print c->recv_underlying   ->  $3 = (Curl_recv *) 0x0
```

Both NULL at crash time — `Curl_http2_switched()` never ran for this connection. Confirmed.

## 3. Fix

Add the symmetric NULL guard the recv path already has. When the underlying transport isn't
wired up yet, we genuinely cannot write, so return `NGHTTP2_ERR_WOULDBLOCK` — the honest
"can't send right now" signal. nghttp2 unwinds cleanly, and we avoid the spurious
`failf(... "Failed sending HTTP2 data")` that `NGHTTP2_ERR_CALLBACK_FAILURE` would log
during teardown.

```c
  (void)h2;
  (void)flags;

  if(!c->send_underlying)
    /* if called "too early", this pointer isn't setup yet! */
    return NGHTTP2_ERR_WOULDBLOCK;

  written = ((Curl_send*)c->send_underlying)(conn, FIRSTSOCKET,
                                             data, length, &result);
```

See `patch.diff` in this directory for the exact diff.

## 4. Verification

Rebuilt with the bug's sanitizer via `arvo compile` (run from `/src/curl_fuzzer` so
`build.sh`'s relative `./ossfuzz.sh` resolves; `arvo` sets `SANITIZER=undefined` for UBSan).
Build exited 0, then re-ran the PoC:

```
Running: /tmp/poc
Executed /tmp/poc in 5 ms          <-- previously: SEGV / ABORTING
ARVO_EXIT: 0
```

The input that triggered the null-deref now runs to completion with no crash.

## Why this is the right (minimal) fix

- It addresses the **root cause** (an unset function pointer being dereferenced), not the symptom.
- It is **symmetric** with the existing, intentional recv-path guard — same condition, same
  "called too early" rationale — so it matches the surrounding code's established pattern.
- It is **4 lines** and changes no build flags, no control flow beyond the early-return on the
  not-yet-initialized state.
