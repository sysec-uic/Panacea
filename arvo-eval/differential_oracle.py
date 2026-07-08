"""Differential -fix oracle: grade a learned lesson by comparing the agent's
accepted patch against the canonical n132/arvo:{id}-fix image.

Runs ONLY in the learning path (learn_loop), after the agent has produced its
accepted patch. Its output feeds the ledger and the add/suppress/confidence
decision -- never agent feedback. The deployment-faithful -fix wall is preserved.
"""
import json
import re
import subprocess
from pathlib import Path

from build_instance import build_instance
from verify_fix import docker_exec, COMPILE_TIMEOUT, RUN_TIMEOUT, compile_env, env_prefix, apply_patch

PROBE_DIR = Path(__file__).parent / "differential" / "mruby_probes"
GOLDEN_DIR = Path(__file__).parent / "differential" / "golden"
PROBE_RUN_TIMEOUT = 60


def default_probes() -> list[Path]:
    """Committed mruby probe scripts, in deterministic (sorted) order."""
    return sorted(PROBE_DIR.glob("*.rb"))


_NOISE_PATTERNS = [
    re.compile(r"==\d+==.*$", re.MULTILINE),          # sanitizer ==PID== banner lines
    re.compile(r"^SUMMARY:.*$", re.MULTILINE),        # sanitizer summary line
    re.compile(r"0x[0-9a-fA-F]+"),                    # hex addresses
    re.compile(r"\b(?:AddressSanitizer|MemorySanitizer|UndefinedBehaviorSanitizer)\b"),
    re.compile(r"\b(?:pid|tid)[=: ]*\d+", re.IGNORECASE),
    re.compile(r"\bin \d+(?:\.\d+)? ?(?:ms|s)\b"),    # timing fragments
]


def normalize(text: str) -> str:
    """Strip non-deterministic / sanitizer noise so only semantic output remains."""
    out = text
    for pat in _NOISE_PATTERNS:
        out = pat.sub("", out)
    # Collapse trailing whitespace and drop now-empty lines.
    lines = [ln.rstrip() for ln in out.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip())


def outputs_diverge(agent: tuple[int, str], fix: tuple[int, str]) -> str | None:
    """Return 'exit', 'stdout', or None for one probe's agent-vs-fix comparison."""
    agent_exit, agent_out = agent
    fix_exit, fix_out = fix
    if agent_exit != fix_exit:
        return "exit"
    if normalize(agent_out) != normalize(fix_out):
        return "stdout"
    return None


def decide_label(*, fix_image_available: bool, errored: bool, divergences: list) -> str:
    if not fix_image_available:
        return "no_fix_available"
    if errored:
        return "oracle_error"
    if divergences:
        return "divergent"
    return "oracle_confirmed"


class OracleError(Exception):
    """Raised when the grader cannot complete (build/run failure). Mapped to
    the 'oracle_error' label so a flaky grader never costs a real lesson."""


def grade(bug, agent_diff, *, probes=None, script_texts=None,
          patched_container=None, poc_only=False, ops=None) -> dict:
    """Compare the agent-patched build against n132/arvo:{id}-fix.

    Returns {label, fix_image_available, divergences}. The agent never sees this.
    `script_texts` overrides reading `probes` from disk (used by tests); in
    production `probes` defaults to `default_probes()` and their text is read here.
    """
    if ops is None:
        ops = DockerOps()
    local_id = bug["localId"]

    if not ops.fix_image_available(local_id):
        return {"label": "no_fix_available", "fix_image_available": False, "divergences": []}

    if script_texts is None:
        probe_paths = default_probes() if probes is None else list(probes)
        script_texts = [p.read_text() for p in probe_paths]
        script_labels = [p.name for p in probe_paths]
    else:
        script_labels = list(script_texts)

    own_agent = patched_container is None
    agent_c = patched_container
    fix_c = None
    try:
        if own_agent:
            agent_c = ops.build_agent(bug, agent_diff)   # may raise OracleError
        fix_c = ops.start_fix(local_id)

        divergences = []
        a, f = ops.run_poc(agent_c), ops.run_poc(fix_c)
        # PoC stdout contains fuzzer noise we can't fully normalize; only compare
        # exit codes here. Stdout comparison is reserved for the probe scripts.
        if a[0] != f[0]:
            divergences.append({"probe": "poc", "kind": "exit"})

        if not poc_only:
            ops.check_mruby_binary(agent_c)   # raises OracleError if missing
            goldens = ops.get_probe_goldens(bug, local_id, script_texts, script_labels)
            for label, text in zip(script_labels, script_texts):
                a = ops.run_script(agent_c, text)
                g = goldens[label]
                f = (g["exit"], g["stdout"])
                kind = outputs_diverge(a, f)
                if kind:
                    divergences.append({"probe": label, "kind": kind})

        label = decide_label(fix_image_available=True, errored=False, divergences=divergences)
        return {"label": label, "fix_image_available": True, "divergences": divergences}
    except (OracleError, subprocess.SubprocessError) as exc:
        # Intentional bypass of decide_label: the error path carries an extra
        # "error" key, so it builds the oracle_error dict directly. Keep this in
        # sync with decide_label's errored=True branch if that label ever changes.
        return {"label": "oracle_error", "fix_image_available": True,
                "divergences": [], "error": str(exc)}
    finally:
        if own_agent and agent_c:
            ops.cleanup(agent_c)
        if fix_c:
            ops.cleanup(fix_c)


class DockerOps:
    """Real Docker-backed ops. Builds the agent-patched container the same way
    verify_fix does, and runs the prebuilt -fix image directly."""

    def fix_image_available(self, local_id: int) -> bool:
        image = f"n132/arvo:{local_id}-fix"
        if subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0:
            return True
        return subprocess.run(["docker", "pull", image],
                              capture_output=True).returncode == 0

    def build_agent(self, bug: dict, diff: str) -> str:
        instance = build_instance(bug)
        project = instance["project"]
        container = f"arvo-{instance['instance_id']}-oracle-agent"
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(
            ["docker", "run", "-d", "--name", container, instance["image_name"],
             "sleep", str(COMPILE_TIMEOUT + 600)], check=True, capture_output=True)
        try:
            exec_fn = lambda cmd, d=None: docker_exec(container, cmd, input=d, timeout=60)
            apply_res = apply_patch(exec_fn, project, diff)
            if apply_res.returncode != 0:
                raise OracleError(f"agent patch did not apply: {apply_res.stderr[:500]}")
            docker_exec(container,
                        "sed -i 's#/depot_tools/ninja -C#/depot_tools/ninja -j3 -C#g' "
                        "/src/build.sh 2>/dev/null || true", timeout=30)
            env = env_prefix(compile_env(bug))
            build_res = docker_exec(container, f"cd /src/{project} && {env} compile",
                                    timeout=COMPILE_TIMEOUT)
            if build_res.returncode != 0:
                raise OracleError("agent build failed under oracle")
            return container
        except BaseException:
            # Any failure (raise or non-zero handled above) must not leak the
            # container: grade()'s finally can't clean it since it never got the name.
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            raise

    def get_probe_goldens(self, bug: dict, local_id: int,
                          script_texts: list[str], script_labels: list[str]) -> dict:
        """Return cached probe goldens for this bug, building them if not yet stored.

        Goldens are the expected (exit, stdout) for each probe script run against the
        compiled -fix image. They're computed once and saved to differential/golden/
        so future grades never rebuild the fix container for probes.
        """
        golden_path = GOLDEN_DIR / f"{local_id}.json"
        if golden_path.exists():
            return json.loads(golden_path.read_text())

        # Build the fix container and compile so bin/mruby is available.
        container = f"arvo-{local_id}-oracle-fix-build"
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(
            ["docker", "run", "-d", "--name", container, f"n132/arvo:{local_id}-fix",
             "sleep", str(COMPILE_TIMEOUT + 600)], check=True, capture_output=True)
        try:
            docker_exec(container,
                        "sed -i 's#/depot_tools/ninja -C#/depot_tools/ninja -j3 -C#g' "
                        "/src/build.sh 2>/dev/null || true", timeout=30)
            env = env_prefix(compile_env(bug))
            build_res = docker_exec(container, "cd /src/mruby && {env} compile".format(env=env),
                                    timeout=COMPILE_TIMEOUT)
            if build_res.returncode != 0:
                raise OracleError("fix container compile failed when building probe goldens")

            # Sanity gate before running probes.
            chk = docker_exec(container, "test -x /src/mruby/bin/mruby", timeout=10)
            if chk.returncode != 0:
                raise OracleError("fix container missing bin/mruby after compile")

            goldens = {}
            for label, text in zip(script_labels, script_texts):
                r = self.run_script(container, text)
                goldens[label] = {"exit": r[0], "stdout": r[1]}

            GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
            golden_path.write_text(json.dumps(goldens, indent=2))
            return goldens
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)

    def check_mruby_binary(self, container: str) -> None:
        """Raise OracleError if bin/mruby is missing — catches silent infra failures."""
        r = docker_exec(container, "test -x /src/mruby/bin/mruby", timeout=10)
        if r.returncode != 0:
            raise OracleError(f"container {container} missing /src/mruby/bin/mruby after compile")

    def start_fix(self, local_id: int) -> str:
        container = f"arvo-{local_id}-oracle-fix"
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(
            ["docker", "run", "-d", "--name", container, f"n132/arvo:{local_id}-fix",
             "sleep", str(COMPILE_TIMEOUT + 600)], check=True, capture_output=True)
        return container

    def run_poc(self, container: str) -> tuple[int, str]:
        r = docker_exec(container, "arvo", timeout=RUN_TIMEOUT)
        return r.returncode, r.stdout + r.stderr

    def run_script(self, container: str, script_text: str) -> tuple[int, str]:
        r = docker_exec(container, "cd /src/mruby && bin/mruby /dev/stdin",
                        input=script_text, timeout=PROBE_RUN_TIMEOUT)
        return r.returncode, r.stdout + r.stderr

    def cleanup(self, container: str) -> None:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
