"""Bounded, isolated real-container acceptance; never mounts production directories."""

import argparse
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from tests.fixtures import image_pdf, merged_pdf, text_pdf

ROOTS = ("inbox", "consume", "staging", "archive", "review", "work", "state", "model_cache")


def docker(*args, check=True):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=180
    )
    return (result.stdout + (result.stderr if args[0] == "logs" else "")).strip()


def inspect(name):
    return json.loads(docker("inspect", name))[0]


def query(name, sql):
    script = (
        "import json,sqlite3; c=sqlite3.connect('/acceptance/state/jobs.sqlite3'); "
        f"print(json.dumps(c.execute({sql!r}).fetchall()))"
    )
    return json.loads(docker("exec", name, "python", "-c", script))


def await_condition(name, predicate, timeout=300):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        state = inspect(name)["State"]
        if not state["Running"]:
            raise RuntimeError(f"container exited: {state}; logs={docker('logs', name)}")
        if predicate():
            return
        time.sleep(1)
    raise TimeoutError(f"acceptance wait expired; logs={docker('logs', name)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="doc-splitter:acceptance")
    parser.add_argument(
        "--cache", type=Path, help="optional existing real cache; default cold download"
    )
    args = parser.parse_args()
    suffix = uuid.uuid4().hex[:12]
    volume, name = f"split-acceptance-{suffix}", f"split-acceptance-{suffix}"
    docker("volume", "create", volume)
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix="split-synthetic-") as temporary:
            fixtures = Path(temporary)
            text_pdf(fixtures / "SYNTHETIC-single.pdf")
            merged_pdf(fixtures / "SYNTHETIC-multi.pdf")
            text_pdf(fixtures / "SYNTHETIC-41-pages.pdf", 41)
            text_pdf(fixtures / "SYNTHETIC-128-pages.pdf", 128)
            text_pdf(fixtures / "SYNTHETIC-129-rejected.pdf", 129)
            image_pdf(fixtures / "SYNTHETIC-scan.pdf")
            init = (
                "import pathlib,os,shutil; r=pathlib.Path('/acceptance'); "
                f"[(r/p).mkdir(exist_ok=True) for p in {ROOTS!r}]; "
                "files=pathlib.Path('/fixtures').glob('*.pdf'); "
                "[shutil.copy(p,r/'inbox'/p.name) for p in files]; "
                "[os.chown(p,10001,10001) for p in [r,*r.rglob('*')]]"
            )
            docker(
                "run",
                "--rm",
                "--user",
                "0:0",
                "--entrypoint",
                "python",
                "-v",
                f"{volume}:/acceptance",
                "--mount",
                f"type=bind,source={fixtures},target=/fixtures,readonly",
                args.image,
                "-c",
                init,
            )
        if args.cache:
            docker(
                "run",
                "--rm",
                "--user",
                "0:0",
                "--entrypoint",
                "python",
                "-v",
                f"{volume}:/acceptance",
                "--mount",
                f"type=bind,source={args.cache.resolve()},target=/cache,readonly",
                args.image,
                "-c",
                "import shutil,pathlib,os; "
                "shutil.copytree('/cache','/acceptance/model_cache',dirs_exist_ok=True); "
                "files=pathlib.Path('/acceptance/model_cache').rglob('*'); "
                "[os.chown(p,10001,10001) for p in files]",
            )
        options = [
            "--name",
            name,
            "--init",
            "--user",
            "10001:10001",
            "--cpus",
            "2",
            "--memory",
            "4g",
            "--pids-limit",
            "128",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:size=134217728,mode=1777",
            "-v",
            f"{volume}:/acceptance",
            "-e",
            "SPLIT_COMPLETION_MODE=atomic",
        ]
        for root in ROOTS:
            options.extend(["-e", f"SPLIT_{root.upper()}=/acceptance/{root}"])
        started = time.monotonic()
        docker("run", "-d", *options, args.image)
        created = True
        await_condition(
            name,
            lambda: inspect(name)["State"].get("Health", {}).get("Status") == "healthy",
            timeout=1800,
        )
        ready_seconds = time.monotonic() - started
        await_condition(
            name,
            lambda: query(name, "SELECT count(*) FROM jobs WHERE status IN ('completed','review')")[
                0
            ][0]
            == 6,
            timeout=600,
        )
        elapsed = time.monotonic() - started
        rows = query(
            name, "SELECT source_name,status,proposal,error FROM jobs ORDER BY source_name"
        )
        assert sum(row[1] == "completed" for row in rows) == 5, rows
        assert sum(row[1] == "review" for row in rows) == 1, rows
        for source, status, proposal, error in rows:
            if "129" in source:
                assert status == "review" and "128" in error
            else:
                expected = (
                    128
                    if "128" in source
                    else 41
                    if "41" in source
                    else 4
                    if "multi" in source
                    else 1
                )
                assert json.loads(proposal)["page_count"] == expected
        stats = json.loads(docker("stats", "--no-stream", "--format", "{{json .}}", name))
        resource_stats = docker(
            "exec",
            name,
            "python",
            "-c",
            "from pathlib import Path; import json; "
            "r=Path('/sys/fs/cgroup'); "
            "print(json.dumps({p:(r/p).read_text() for p in ('memory.peak','cpu.stat')}))",
        )
        before = query(name, "SELECT job_id,name,status FROM outputs ORDER BY name")
        assert all(row[2] == "published" for row in before)
        docker(
            "exec",
            name,
            "python",
            "-c",
            "from pathlib import Path; "
            "[p.unlink() for p in Path('/acceptance/consume').glob('*.pdf')]",
        )
        docker("stop", "--time", "30", name)
        docker("rm", name)
        created = False
        # Restart the same warmed volumes without any network; consumed files stay absent.
        docker("run", "-d", *options, "--network", "none", args.image)
        created = True
        await_condition(
            name, lambda: inspect(name)["State"].get("Health", {}).get("Status") == "healthy"
        )
        assert query(name, "SELECT job_id,name,status FROM outputs ORDER BY name") == before
        assert (
            docker(
                "exec",
                name,
                "python",
                "-c",
                "from pathlib import Path; print(len(list(Path('/acceptance/consume').iterdir())))",
            )
            == "0"
        )
        # Terminate the actual worker during a fresh 41-page inference, then resume.
        docker(
            "exec",
            name,
            "python",
            "-c",
            "import pathlib; p=pathlib.Path('/acceptance/inbox/SYNTHETIC-41-pages.pdf'); "
            "q=p.with_name('SYNTHETIC-restart.pdf'); "
            "q.write_bytes(p.read_bytes()+b'\\n% restart test\\n')",
        )
        await_condition(
            name,
            lambda: query(name, "SELECT count(*) FROM jobs WHERE status='preparing'")[0][0] == 1,
        )
        worker_pid = docker(
            "exec",
            name,
            "python",
            "-c",
            "import json; print(json.load(open('/acceptance/state/health.json'))['pid'])",
        )
        docker(
            "exec",
            name,
            "python",
            "-c",
            f"import os,signal; os.kill({int(worker_pid)},signal.SIGKILL)",
            check=False,
        )
        # PID1 init exits after its worker dies; restart only that isolated acceptance container.
        assert docker("wait", name) == "137"
        docker("start", name)
        await_condition(
            name,
            lambda: query(name, "SELECT count(*) FROM jobs WHERE status='completed'")[0][0] == 6,
            timeout=600,
        )
        attempts = query(
            name, "SELECT attempts FROM jobs WHERE source_name='SYNTHETIC-restart.pdf'"
        )[0][0]
        assert attempts == 2, attempts
        report = {
            "image": inspect(name)["Image"],
            "model_cache": "prepopulated" if args.cache else "cold automated download",
            "healthy_seconds": round(ready_seconds, 3),
            "initial_fixtures_total_seconds": round(elapsed, 3),
            "container_stats_after_processing": stats,
            "cgroup_resources_after_initial_fixtures": json.loads(resource_stats),
            "fixtures": [(row[0], row[1]) for row in rows],
            "offline_restart": "passed",
            "consumed_outputs_not_regenerated": True,
            "forced_inference_termination_resumed_attempts": attempts,
            "logs": docker("logs", name),
        }
        print(json.dumps(report, indent=2))
    finally:
        if created:
            docker("rm", "--force", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
