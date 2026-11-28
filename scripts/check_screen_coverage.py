#!/usr/bin/env python3
"""Report measured production coverage without counting inline Rust test code.

Enforce 95% for each screen-state module and for changed executable runtime
lines. Whole-workspace coverage is also reported, with no claim that existing
unrelated platform/authentication/audio modules meet the feature threshold.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FEATURE_FILES = (
    "scripts/linux-accessibility.py",
    "apps/socket_stream_desktop/ui/source_preflight.py",
    "crates/socket_stream_core/src/a11y_diff.rs",
    "crates/socket_stream_core/src/conversation.rs",
    "apps/socket_stream/src/live_context.rs",
)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


def source_limit(path):
    """These Rust modules keep their inline test modules after production code."""
    if not path.endswith(".rs"):
        return None
    source = (ROOT / path).read_text().splitlines()
    for index, line in enumerate(source, 1):
        if line.strip() == "#[cfg(test)]" and index < len(source):
            if source[index].strip().startswith("mod "):
                return index
    return None


def load_rust(path):
    result = {}
    current = None
    for line in Path(path).read_text().splitlines():
        if line.startswith("SF:"):
            absolute = Path(line[3:]).resolve()
            try:
                current = str(absolute.relative_to(ROOT))
            except ValueError:
                current = None
            if current:
                result.setdefault(current, {})
        elif current and line.startswith("DA:"):
            number, count, *_ = line[3:].split(",")
            old = result[current].get(int(number), False)
            result[current][int(number)] = old or int(count) > 0
    for name, lines in result.items():
        limit = source_limit(name)
        if limit:
            result[name] = {line: hit for line, hit in lines.items() if line < limit}
    return result


def load_python(path):
    result = {}
    for name, value in json.loads(Path(path).read_text())["files"].items():
        absolute = (ROOT / name).resolve()
        try:
            relative = str(absolute.relative_to(ROOT))
        except ValueError:
            continue
        result[relative] = {line: True for line in value["executed_lines"]}
        result[relative].update({line: False for line in value["missing_lines"]})
    return result


def summary(lines):
    total = len(lines)
    covered = sum(lines.values())
    return {"covered": covered, "executable": total,
            "line_percent": round(100 * covered / total, 2) if total else 100.0,
            "missing_lines": sorted(line for line, hit in lines.items() if not hit)}


def runtime_path(name):
    return (name.endswith(".rs") and (name.startswith("crates/") or name.startswith("apps/socket_stream/src/"))) or (
        name.endswith(".py") and (name.startswith("apps/socket_stream_desktop/ui/") or name == "scripts/linux-accessibility.py"))


def declarations_only(name):
    """A Rust module index has no executable lines for LLVM to instrument."""
    return name.endswith(".rs") and all(
        not line.strip() or line.lstrip().startswith("//")
        or re.fullmatch(r"\s*#\[.*\]\s*", line)
        or re.fullmatch(r"\s*(?:pub\s+)?mod\s+\w+;\s*", line)
        for line in (ROOT / name).read_text().splitlines())


def changed_lines(base, name, measured):
    exists = subprocess.run(["git", "cat-file", "-e", f"{base}:{name}"],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not exists:
        return set(measured)
    diff = git("diff", "--unified=0", base, "--", name)
    added = set()
    for start, count in re.findall(r"^@@ -[^ ]+ \+(\d+)(?:,(\d+))? @@", diff, flags=re.MULTILINE):
        added.update(range(int(start), int(start) + (int(count) if count else 1)))
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust", default="target/screen-coverage/rust.lcov")
    parser.add_argument("--python", default="target/screen-coverage/python.json")
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--minimum", type=float, default=95.0)
    parser.add_argument("--output", default="target/screen-coverage/summary.json")
    args = parser.parse_args()
    rust = load_rust(ROOT / args.rust)
    python = load_python(ROOT / args.python)
    all_files = {**rust, **python}
    features = {name: summary(all_files.get(name, {})) for name in FEATURE_FILES}
    # A missing instrumented file must never be reported as a passing empty set.
    failures = [name for name in FEATURE_FILES if not all_files.get(name)]
    changed = {}
    declarations = []
    paths = set(git("diff", "--name-only", "--diff-filter=AM", args.base_ref).splitlines())
    paths.update(git("ls-files", "--others", "--exclude-standard").splitlines())
    for name in sorted(paths):
        if not runtime_path(name):
            continue
        measured = all_files.get(name, {})
        if not measured:
            if declarations_only(name):
                declarations.append(name)
                continue
            failures.append(f"unmeasured runtime file: {name}")
            continue
        lines = changed_lines(args.base_ref, name, measured)
        changed[name] = {line: hit for line, hit in measured.items() if line in lines}
    changed_totals = {f"{name}:{line}": hit for name, lines in changed.items() for line, hit in lines.items()}
    workspace = {}
    for label, files in (("rust", rust), ("python", python)):
        totals = {f"{name}:{line}": hit for name, lines in files.items() for line, hit in lines.items()}
        workspace[label] = summary(totals)
        workspace[label].pop("missing_lines")
    changed_total = summary(changed_totals)
    changed_total.pop("missing_lines")
    report = {
        "base_commit": git("rev-parse", args.base_ref).strip(),
        "threshold": args.minimum,
        "scope": "Production executable lines; inline Rust test modules excluded. Runtime changes measured against base_commit. Shell/setup scripts have syntax checks, not line instrumentation.",
        "feature_modules": features,
        "changed_runtime_total": changed_total,
        "changed_runtime_files": {name: summary(lines) for name, lines in changed.items()},
        "non_executable_module_indexes": declarations,
        "whole_measured_workspace": workspace,
    }
    for name, metric in features.items():
        print(f"{metric['line_percent']:6.2f}%  {metric['covered']:4}/{metric['executable']:<4}  {name}")
        if metric["line_percent"] < args.minimum:
            failures.append(name)
    print(f"{changed_total['line_percent']:6.2f}%  changed runtime lines ({changed_total['covered']}/{changed_total['executable']})")
    if not changed_totals or changed_total["line_percent"] < args.minimum:
        failures.append("changed runtime lines")
    for name, metric in workspace.items():
        print(f"{metric['line_percent']:6.2f}%  whole measured {name} workspace (reported, not the feature gate)")
    report["passed"] = not failures
    report["failures"] = failures
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    if failures:
        print("Coverage gate failed: " + "; ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
