# Application workflows

`yt_workflows` provides synchronous model evaluation and application helpers,
outside the two business libraries. Its source is in `src/yt_workflows`, packaged
by the root `yt-finder` project with its single `.venv`/`uv.lock`.

`run_evaluation(EvaluationRequest(...), on_progress=...)` returns a report dictionary
and emits structured progress. Request types import only the standard library;
workflows do not import either UI, print/argparse/exit, or change caller environment.

Evaluation freezes one schema-v2 window snapshot and runs model subprocesses
sequentially with the root interpreter. Children retain the caller's process group
for cancellation; offline options go through the child environment. Path checks
protect input/cache/snapshot/report and aliases before writes. A runner hook permits
model-free tests. Metrics retain the partial-label caveat in
[evaluation/README.md](../evaluation/README.md).

`logfiles.LockedRotatingFileHandler` supplies POSIX process locking around app
log reopen/write/rotation and configures no loggers itself. Setup and commands:
[README root](../README.md).
