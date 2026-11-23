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
import multiprocessing
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, Protocol

from tthfv import build as build_mod
from tthfv import fetch as fetch_mod
from tthfv import manifest, paths
from tthfv import reference as reference_mod
from tthfv.errors import UserError
from tthfv.manifest import Design

DEFAULT_JOBS = 40
MAX_JOBS = 80
STAGES = ("fetch", "build", "reference", "all")
_ORDER = ("fetch", "build", "reference")


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
    ops: Ops, stage: str, design_id: str, force: bool, clocks: int | None, error_log: Path
) -> tuple[dict[str, str], float]:
    """Run one design's stages in a worker. Returns ({stage: status}, seconds); never raises."""
    started = time.monotonic()
    wanted = _ORDER if stage == "all" else (stage,)
    statuses: dict[str, str] = {}
    problems: list[str] = []

    def attempt(name: str, call) -> str:
        try:
            return call().status
        except Exception:  # noqa: BLE001 - one design's failure must not stop the run
            problems.append(f"--- {name} ---\n{traceback.format_exc()}")
            return "error"

    # Everything needs the fetch; only a forced fetch/all run fetches again.
    fetched = attempt("fetch", lambda: ops.fetch(design_id, force and stage in ("fetch", "all")))
    if "fetch" in wanted:
        statuses["fetch"] = fetched
    if fetched == "ok":
        if "build" in wanted:
            statuses["build"] = attempt("build", lambda: ops.build(design_id, force))
        if "reference" in wanted:
            statuses["reference"] = attempt("reference", lambda: ops.reference(design_id, force, clocks))
    else:
        # A fetch that raised is reported as an error on the stage the user asked for.
        blocked = "error" if fetched == "error" and "fetch" not in wanted else "not-run"
        for name in wanted:
            statuses.setdefault(name, blocked)
    if problems:
        error_log.parent.mkdir(parents=True, exist_ok=True)
        error_log.write_text(f"{design_id}\n" + "\n".join(problems))
    else:
        error_log.unlink(missing_ok=True)
    return statuses, time.monotonic() - started


@dataclass
class RunSummary:
    total: int
    seconds: float
    workers: int
    counts: dict[str, dict[str, int]]  # stage -> status -> number of designs
    log: Path

    def line(self) -> str:
        def part(c: dict[str, int]) -> str:
            return ", ".join(f"{n} {s}" for s, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

        if len(self.counts) == 1:
            body = part(next(iter(self.counts.values())))
        else:
            body = "; ".join(f"{stage} {part(c)}" for stage, c in self.counts.items())
        return f"done {self.total} designs in {self.seconds:.0f}s: {body}"


def _eta(done: int, total: int, spent: float, workers: int) -> str:
    """Local clock time at which the run should finish: average seconds per finished design times
    the designs left, spread over the workers."""
    left = total - done
    finish = time.time() + (spent / done) * left / max(1, min(workers, left or 1))
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
    workers = max(1, min(clamp_jobs(jobs), len(ids)))
    shown = _ORDER if stage == "all" else (stage,)
    counts: dict[str, collections.Counter] = {s: collections.Counter() for s in shown}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    work = paths.WORK
    work.mkdir(parents=True, exist_ok=True)
    log = work / f"run-{stage}-{stamp}.log"
    started = time.monotonic()
    spent = 0.0

    with log.open("a") as logf:

        def emit(text: str) -> None:
            print(text, flush=True)
            logf.write(text + "\n")
            logf.flush()

        ctx = multiprocessing.get_context("forkserver")
        with cf.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            futures = {
                pool.submit(_guarded, ops, stage, i, force, clocks, work / "run" / i / "error.log"): i
                for i in ids
            }
            for n, fut in enumerate(cf.as_completed(futures), 1):
                design_id = futures[fut]
                try:
                    statuses, seconds = fut.result()
                except Exception:  # noqa: BLE001 - the worker itself died, e.g. was killed
                    log_path = work / "run" / design_id / "error.log"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(f"{design_id}\n{traceback.format_exc()}")
                    statuses, seconds = {s: "error" for s in shown}, 0.0
                for s in shown:
                    counts[s][statuses[s]] += 1
                spent += seconds
                joined = ",".join(statuses[s] for s in shown)
                emit(
                    f"{n}/{len(ids)} {design_id} {joined} {seconds:.0f}s eta {_eta(n, len(ids), spent, workers)}"
                )
        summary = RunSummary(
            len(ids), time.monotonic() - started, workers, {s: dict(c) for s, c in counts.items()}, log
        )
        emit(summary.line())
    return summary


def _cmd_run(args: argparse.Namespace) -> int:
    designs = manifest.load(extras=manifest.EXTRAS_DEFAULT)
    ids = select(designs, args.only or [], args.candidates)
    if not ids:
        raise UserError("no designs selected")
    run(args.stage, ids, jobs=args.jobs, force=args.force, clocks=args.clocks, ops=REAL_OPS)
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("run", help="run a stage over many designs in parallel")
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--only", action="append", metavar="ID", help="<shuttle>/<macro>; repeatable")
    p.add_argument("--candidates", action="store_true", help="only designs whose render verdict is ok")
    p.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"parallel designs (default {DEFAULT_JOBS}, at most {MAX_JOBS})",
    )
    p.add_argument("--force", action="store_true", help="do the stage again even if already done")
    p.add_argument("--clocks", type=int, default=None, help="clocks to simulate (reference)")
    p.set_defaults(func=_cmd_run)
