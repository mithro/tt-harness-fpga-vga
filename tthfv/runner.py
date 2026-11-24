# SPDX-License-Identifier: Apache-2.0
"""Run a stage over many designs in parallel: `tthfv run <fetch|build|reference|all>`.

One worker process handles one design end to end, so a design's stages never overlap. For `all` the
order is fetch, then build and reference (both need the fetch; a failed fetch makes them not-run).
Workers are started with the forkserver method and are handed only a design id and options: they
load the manifest themselves, so nothing unpicklable crosses the process boundary.

Finished work is skipped by the stages themselves (fetch.json, build.json, reference.json) unless
--force. One design's failure, even an exception, is recorded as status `error` with the traceback in
WORK/run/<id>/error.log, and the run goes on. Progress is one line per finished design, written and
flushed to WORK/run-<stage>-<UTC timestamp>.log and to stdout; the last line is the summary.
"""

import argparse
import collections
import concurrent.futures as cf
import functools
import json
import multiprocessing
import os
import signal
import time
import traceback
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, Protocol

from tthfv import build as build_mod
from tthfv import fetch as fetch_mod
from tthfv import limits as limits_mod
from tthfv import manifest, paths, safefile, sandbox
from tthfv import reference as reference_mod
from tthfv.errors import UserError
from tthfv.manifest import Design
from tthfv.text import strip_control

DEFAULT_JOBS = 4  # only a few designs are built at a time: each may use its whole cgroup (see tthfv.limits)
MAX_JOBS = 16
MAX_MEMORY_SHARE = (
    0.5  # --jobs x MemoryMax may not exceed this share of the machine's memory without --force-jobs
)
STAGES = ("fetch", "build", "reference", "all")
_ORDER = ("fetch", "build", "reference")
INFLIGHT = "inflight"  # in a design's run directory while a worker has it


class Outcome(NamedTuple):
    status: str
    reason: str = ""


class Ops(Protocol):
    """What a worker calls for each stage. Must be picklable; tests supply fakes."""

    def fetch(self, design_id: str, force: bool) -> Outcome: ...
    def build(self, design_id: str, force: bool) -> Outcome: ...
    def reference(self, design_id: str, force: bool, clocks: int | None) -> Outcome: ...


@functools.lru_cache(maxsize=1)
def _designs() -> list[Design]:
    return manifest.load(extras=manifest.EXTRAS_DEFAULT)


class RealOps:
    """The real stages. Each call loads its design from the manifest (loaded once per process)."""

    def fetch(self, design_id: str, force: bool) -> Outcome:
        r = fetch_mod.fetch(manifest.by_id(_designs(), design_id), force=force)
        return Outcome(r.status, r.reason)

    def build(self, design_id: str, force: bool) -> Outcome:
        d = manifest.by_id(_designs(), design_id)
        r = build_mod.build(d, fetch_mod.fetch(d), force=force)
        return Outcome(r.status, r.reason)

    def reference(self, design_id: str, force: bool, clocks: int | None) -> Outcome:
        r = reference_mod.make_reference(manifest.by_id(_designs(), design_id), clocks, force)
        return Outcome(r.status, r.reason)


REAL_OPS: Ops = RealOps()


def clamp_jobs(jobs: int) -> int:
    return max(1, min(jobs, MAX_JOBS))


def _total_memory() -> int:
    """Physical memory in bytes, from /proc/meminfo."""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise UserError("cannot read MemTotal from /proc/meminfo")


def commitment(jobs: int, force_jobs: bool = False) -> str:
    """For more jobs than the default: the line saying how much memory and CPU `jobs` designs could commit at
    once against the machine's total. Raises UserError if the memory limits add up to more than MAX_MEMORY_SHARE
    of physical memory and force_jobs is not given. Returns '' when jobs is within the default."""
    if jobs <= DEFAULT_JOBS:
        return ""
    cpus = os.cpu_count() or 1
    record = sandbox.limits_record()
    if record is None:
        return (
            f"{jobs} jobs with no resource limits ({sandbox.ENV_VAR} or {limits_mod.ENV_MODE} is none): "
            f"nothing bounds what they use of the machine's {cpus} CPUs and {_total_memory() >> 30} GiB"
        )
    limits = limits_mod.Limits(record["memory"], record["tasks"], record["cpu"])
    memory = jobs * limits.memory_bytes
    cores = jobs * int(limits.cpu.rstrip("%")) / 100
    total = _total_memory()
    line = (
        f"{jobs} jobs x {limits.memory} memory = up to {memory / (1 << 30):.0f} GiB of the machine's "
        f"{total / (1 << 30):.0f} GiB, and {jobs} x {limits.cpu} CPU = up to {cores:g} of its {cpus} cores"
    )
    if memory > MAX_MEMORY_SHARE * total and not force_jobs:
        raise UserError(
            f"{line}: more than {MAX_MEMORY_SHARE:.0%} of the memory. Use fewer --jobs, a lower "
            "TTHFV_LIMIT_MEMORY, or --force-jobs"
        )
    return line


def select(designs: list[Design], only: list[str], candidates: bool) -> list[str]:
    """The ids to run, sorted: --only restricts to those ids (unknown ids are a UserError),
    --candidates to designs whose render verdict is ok; both together is the intersection."""
    chosen = designs
    if only:
        wanted = {manifest.by_id(designs, i).id for i in only}
        chosen = [d for d in chosen if d.id in wanted]
    if candidates:
        chosen = [d for d in chosen if d.candidate]
    return sorted(d.id for d in chosen)


def _guarded(
    ops: Ops, stage: str, design_id: str, force: bool, clocks: int | None, run_dir: Path
) -> tuple[dict[str, str], float]:
    """Run one design's stages in a worker. Returns ({stage: status}, seconds); never raises.

    Leaves run_dir/outcome.json (status and reason of each stage that was attempted, merged with
    earlier runs) for `tthfv results`, and run_dir/error.log with the tracebacks if anything raised."""
    started = time.monotonic()
    run_dir.mkdir(parents=True, exist_ok=True)
    safefile.write_file(
        run_dir / INFLIGHT, str(os.getpid())
    )  # lets the parent tell who was running if we die
    wanted = _ORDER if stage == "all" else (stage,)
    outcomes: dict[str, Outcome] = {}
    problems: list[str] = []

    def attempt(name: str, call) -> Outcome:
        try:
            return Outcome(*call())
        except Exception as e:  # noqa: BLE001 - one design's failure must not stop the run
            problems.append(f"--- {name} ---\n{traceback.format_exc()}")
            return Outcome("error", f"{type(e).__name__}: {e}")

    # Everything needs the fetch; only a forced fetch/all run fetches again.
    fetched = attempt("fetch", lambda: ops.fetch(design_id, force and stage in ("fetch", "all")))
    outcomes["fetch"] = fetched
    if fetched.status == "ok":
        if "build" in wanted:
            outcomes["build"] = attempt("build", lambda: ops.build(design_id, force))
        if "reference" in wanted:
            outcomes["reference"] = attempt("reference", lambda: ops.reference(design_id, force, clocks))
    else:
        # A fetch that raised is reported as an error on the stage the user asked for.
        raised = fetched.status == "error" and "fetch" not in wanted
        for name in wanted:
            if name != "fetch":
                outcomes[name] = (
                    Outcome("error", fetched.reason) if raised else Outcome("not-run", "fetch failed")
                )

    run_dir.mkdir(parents=True, exist_ok=True)
    _write_outcomes(run_dir, outcomes)
    if problems:
        safefile.write_file(run_dir / "error.log", f"{design_id}\n" + "\n".join(problems))
    else:
        (run_dir / "error.log").unlink(missing_ok=True)
    (run_dir / INFLIGHT).unlink(missing_ok=True)
    return {name: outcomes[name].status for name in wanted}, time.monotonic() - started


def _write_outcomes(run_dir: Path, outcomes: dict[str, Outcome]) -> None:
    path = run_dir / "outcome.json"
    try:
        record = json.loads(safefile.read_text(path))
    except (OSError, ValueError, safefile.NotPlainFile):
        record = {}
    for name, o in outcomes.items():  # stages not in outcomes keep their entry, and its time
        record[name] = {
            "status": o.status,
            "reason": strip_control(o.reason),
            "finished": datetime.now(UTC).isoformat(timespec="microseconds"),
        }
    safefile.write_file(path, json.dumps(record, indent=2) + "\n")


@dataclass
class RunSummary:
    total: int
    seconds: float
    workers: int
    counts: dict[str, dict[str, int]]  # stage -> status -> number of designs
    log: Path
    finished: int = 0
    interrupted: bool = False

    def line(self) -> str:
        def part(c: dict[str, int]) -> str:
            return ", ".join(f"{n} {s}" for s, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

        if len(self.counts) == 1:
            body = part(next(iter(self.counts.values())))
        else:
            body = "; ".join(f"{stage} {part(c)}" for stage, c in self.counts.items())
        if self.interrupted:
            return f"interrupted after {self.finished}/{self.total} designs in {self.seconds:.0f}s: {body}"
        return f"done {self.total} designs in {self.seconds:.0f}s: {body}"


def _eta(left: int, timed: int, spent: float, workers: int) -> str:
    """Local clock time at which the run should finish: the average seconds of the `timed` designs
    (those that took a second or more; skipped ones would make it far too short) times the designs
    left, spread over the workers."""
    average = spent / timed if timed else 0.0
    finish = time.time() + average * left / max(1, min(workers, left or 1))
    return time.strftime("%H:%M", time.localtime(finish))


def run(
    stage: str,
    ids: list[str],
    *,
    jobs: int = DEFAULT_JOBS,
    force: bool = False,
    clocks: int | None = None,
    ops: Ops | None = None,
) -> RunSummary:
    if stage not in STAGES:
        raise UserError(f"unknown stage {stage!r}; choose from {', '.join(STAGES)}")
    ops = ops if ops is not None else REAL_OPS
    bad: dict[
        str, str
    ] = {}  # designs whose run directory is a symlink: each one's own error, the run goes on
    for i in ids:
        try:
            paths.design_dir("run", i)
        except UserError as e:
            bad[i] = str(e)
    todo = [i for i in ids if i not in bad]
    workers = max(1, min(clamp_jobs(jobs), len(todo)))
    shown = _ORDER if stage == "all" else (stage,)
    counts: dict[str, collections.Counter] = {s: collections.Counter() for s in shown}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    work = paths.WORK
    work.mkdir(parents=True, exist_ok=True)
    log = work / f"run-{stage}-{stamp}.log"
    for n in range(2, 1000):  # a second run in the same second gets its own file
        try:
            logf = log.open("x")
            break
        except FileExistsError:
            log = work / f"run-{stage}-{stamp}-{n}.log"
    else:
        raise UserError(f"cannot create a run log in {work}")
    started = time.monotonic()
    spent = 0.0
    timed = 0
    finished = 0

    def run_dir(design_id: str) -> Path:
        return paths.design_dir("run", design_id)

    with logf:

        def emit(text: str) -> None:
            print(text, flush=True)
            logf.write(text + "\n")
            logf.flush()

        def record(design_id: str, statuses: dict[str, str], seconds: float) -> None:
            nonlocal spent, timed, finished
            finished += 1
            for s in shown:
                counts[s][statuses[s]] += 1
            if seconds >= 1:
                spent += seconds
                timed += 1
            joined = ",".join(statuses[s] for s in shown)
            emit(
                f"{finished}/{len(ids)} {design_id} {joined} {seconds:.0f}s"
                f" eta {_eta(len(ids) - finished, timed, spent, workers)}"
            )

        def died(design_id: str, why: str, detail: str) -> None:
            """Record a design whose worker was lost: error on every stage asked for."""
            run_dir(design_id).mkdir(parents=True, exist_ok=True)
            _write_outcomes(run_dir(design_id), {s: Outcome("error", why) for s in shown})
            safefile.write_file(run_dir(design_id) / "error.log", f"{design_id}\n{why}\n{detail}")
            (run_dir(design_id) / INFLIGHT).unlink(missing_ok=True)
            record(design_id, {s: "error" for s in shown}, 0.0)

        def batch(batch_ids: list[str], batch_workers: int) -> tuple[set[str], bool]:
            """Run designs in a fresh pool. Returns (ids that finished, whether the pool broke)."""
            for i in batch_ids:
                (run_dir(i) / INFLIGHT).unlink(missing_ok=True)
            ctx = multiprocessing.get_context("forkserver")
            pool = cf.ProcessPoolExecutor(max_workers=batch_workers, mp_context=ctx)
            done: set[str] = set()
            broke = interrupted = False
            try:
                futures = {
                    pool.submit(_guarded, ops, stage, i, force, clocks, run_dir(i)): i for i in batch_ids
                }
                for fut in cf.as_completed(futures):
                    design_id = futures[fut]
                    try:
                        statuses, seconds = fut.result()
                    except BrokenProcessPool:
                        broke = True
                        continue
                    except Exception:  # noqa: BLE001 - the worker itself failed
                        died(design_id, "worker failed", traceback.format_exc())
                    else:
                        record(design_id, statuses, seconds)
                    done.add(design_id)
            except KeyboardInterrupt:
                interrupted = True
                _kill_pool(pool)
                raise
            finally:
                pool.shutdown(wait=not (broke or interrupted), cancel_futures=True)
            return done, broke

        def schedule() -> None:
            pending = list(todo)
            suspects: list[str] = []  # in flight when a pool broke: each gets one retry, alone
            breaks = 0
            while pending:
                done, broke = batch(pending, workers)
                pending = [i for i in pending if i not in done]
                if not broke:
                    break
                breaks += 1
                if breaks > len(ids):  # no progress is possible; never loop for ever
                    for i in pending:
                        died(i, "worker died", "the worker pool kept breaking")
                    break
                lost = [i for i in pending if (run_dir(i) / INFLIGHT).exists()]
                suspects.extend(lost)
                pending = [i for i in pending if i not in lost]
            for i in suspects:
                _, broke = batch([i], 1)
                if broke:
                    died(
                        i, "worker died", "the worker process was killed or crashed while running this design"
                    )

        for i, why in bad.items():
            emit(f"{i}: error: {strip_control(why)}")
            record(i, {s: "error" for s in shown}, 0.0)
        interrupted = False
        try:
            schedule()
        except KeyboardInterrupt:
            interrupted = True
            for i in todo:  # designs that were running are abandoned; leave no marker behind
                (run_dir(i) / INFLIGHT).unlink(missing_ok=True)
        summary = RunSummary(
            len(ids),
            time.monotonic() - started,
            workers,
            {s: dict(c) for s, c in counts.items()},
            log,
            finished,
            interrupted,
        )
        emit(summary.line())
    return summary


def _descendants(pid: int) -> list[int]:
    """All processes below pid, from /proc (Linux), whatever session or process group they are in."""
    parent_of: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                parent_of[int(entry.name)] = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue  # gone already
    found, todo = [], [pid]
    while todo:
        cur = todo.pop()
        kids = [p for p, parent in parent_of.items() if parent == cur]
        found += kids
        todo += kids
    return found


def _kill_pool(pool: cf.ProcessPoolExecutor) -> None:
    """Stop a pool now: kill its workers and everything they started (yosys, nextpnr, make, ...)."""
    workers = list(getattr(pool, "_processes", None) or {})
    victims = workers + [d for w in workers for d in _descendants(w)]
    pool.shutdown(wait=False, cancel_futures=True)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _cmd_run(args: argparse.Namespace) -> int:
    sandbox.announce()  # once, here, not in each worker; and also if every result turns out to be cached
    designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    ids = select(designs, args.only or [], args.candidates)
    if not ids:
        raise UserError("no designs selected")
    if args.jobs > MAX_JOBS:
        raise UserError(f"--jobs is at most {MAX_JOBS}")
    if line := commitment(args.jobs, args.force_jobs):
        print(line, flush=True)
    if removed := safefile.empty_trash(paths.WORK / "trash"):  # what earlier stages could not delete
        print(f"removed {removed} entries from {paths.WORK / 'trash'}", flush=True)
    done = run(args.stage, ids, jobs=args.jobs, force=args.force, clocks=args.clocks, ops=REAL_OPS)
    return 130 if done.interrupted else 0


def add_parser(sub) -> None:
    p = sub.add_parser("run", help="run a stage over many designs in parallel")
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--only", action="append", metavar="ID", help="<shuttle>/<macro>; repeatable")
    p.add_argument("--candidates", action="store_true", help="only designs whose render verdict is ok")
    p.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"parallel designs (default {DEFAULT_JOBS}, at most {MAX_JOBS}); above {DEFAULT_JOBS} the memory "
        "and CPU the limits could commit are printed",
    )
    p.add_argument(
        "--force-jobs", action="store_true",
        help=f"allow --jobs x TTHFV_LIMIT_MEMORY above {MAX_MEMORY_SHARE * 100:.0f}%% of the machine's memory",
    )  # fmt: skip
    p.add_argument("--force", action="store_true", help="do the stage again even if already done")
    p.add_argument("--clocks", type=int, default=None, help="clocks to simulate (reference)")
    p.set_defaults(func=_cmd_run)
