#!/usr/bin/env python3
"""
SWE-bench Batch Evaluation Runner

Runs ATROPOS against SWE-bench tasks and collects patches.
Fixed to properly handle ATROPOS refusal/failure outputs and filter patch bloat.
"""

import os
import sys
import json
import requests
import time
import subprocess
import shutil
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Paths adapted for GitHub Actions runner
WORKSPACE_DIR = os.path.abspath("tmp_workspace")
OUTPUT_DIR = os.path.abspath("predictions/swe-bench-full")
PROMPT_DIR = os.path.abspath("tmp_prompts")
JAR_PATH = os.path.abspath("ATROPOS.jar")
PREDICTIONS_FILE = os.path.join(OUTPUT_DIR, "predictions.jsonl")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(PROMPT_DIR, exist_ok=True)

# Regex patterns for ATROPOS refusal/failure outputs
REFUSAL_PATTERNS = [
    r'\[refused\]',
    r'provider context refused',
    r'failed',
    r'SOURCE_PACK_REFUSED',
    r'SOURCE_BINDING_UNAVAILABLE',
    r'SOURCE_PACK_TRUNCATED',
    r'SOURCE_PACK_EMPTY',
    r'attestation refused',
    r'context attestation refused',
]

# Compile patterns for efficiency
REFUSAL_REGEX = [re.compile(p, re.IGNORECASE) for p in REFUSAL_PATTERNS]


def check_atropos_output_for_refusal(stdout: str, stderr: str) -> Optional[str]:
    """
    Check if ATROPOS output indicates a refusal or failure.
    Returns the refusal reason if found, None otherwise.
    """
    combined = f"{stdout}\n{stderr}"
    for pattern in REFUSAL_REGEX:
        match = pattern.search(combined)
        if match:
            # Extract surrounding context for better error reporting
            start = max(0, match.start() - 100)
            end = min(len(combined), match.end() + 200)
            context = combined[start:end].strip()
            return f"ATROPOS refusal detected: {match.group()} (context: ...{context}...)"
    return None


def run_git_diff_filtered(workspace: Path) -> Tuple[str, bool]:
    """
    Run git diff with explicit exclusions to filter out runtime artifacts.
    Returns (patch_content, success)
    """
    # Exclude runtime artifacts and ATROPOS internal directories
    exclusions = [
        ':!.atropos',
        ':!*.jar',
        ':!*.zip',
        ':!*.pyc',
        ':!__pycache__',
        ':!build/',
        ':!.gradle/',
        ':!*.log',
        ':!compile.log',
        ':!compile_log.zip',
        ':!installers.zip',
        ':!ATROPOS.jar',
        ':!ATROPOS-JAR.zip',
    ]

    cmd = ["git", "diff", "--no-color"] + exclusions + ["--", "."]

    try:
        result = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode != 0:
            return f"git diff failed: {result.stderr}", False
        return result.stdout, True
    except subprocess.TimeoutExpired:
        return "git diff timed out", False
    except Exception as e:
        return f"git diff error: {e}", False


def process_instance(instance: Dict, index: int, total: int) -> Dict:
    """
    Process a single SWE-bench instance.
    Returns a result dictionary with status and patch info.
    """
    instance_id = instance['instance_id']
    repo = instance['repo']
    base_commit = instance['base_commit']
    problem_statement = instance['problem_statement']
    patch_file = os.path.join(OUTPUT_DIR, f"{instance_id}.patch")

    if os.path.exists(patch_file):
        return {
            "instance_id": instance_id,
            "status": "SKIPPED",
            "patch_file": patch_file,
            "message": "Already processed"
        }

    print(f"\n[{index+1}/{total}] Processing {instance_id}...")

    # Clean and create workspace
    if os.path.exists(WORKSPACE_DIR):
        shutil.rmtree(WORKSPACE_DIR, ignore_errors=True)
    os.makedirs(WORKSPACE_DIR, exist_ok=True)

    repo_url = f"https://github.com/{repo}.git"
    try:
        subprocess.run(["git", "clone", repo_url, WORKSPACE_DIR], check=True, capture_output=True, text=True, timeout=120)
        subprocess.run(["git", "checkout", base_commit], cwd=WORKSPACE_DIR, check=True, capture_output=True, text=True, timeout=60)
    except subprocess.CalledProcessError as e:
        error_msg = f"Git clone/checkout failed: {e.stderr if e.stderr else str(e)}"
        print(f"    [ERROR] {error_msg}")
        return {
            "instance_id": instance_id,
            "status": "FAILED",
            "patch_file": None,
            "message": error_msg
        }
    except subprocess.TimeoutExpired:
        error_msg = "Git clone/checkout timed out"
        print(f"    [ERROR] {error_msg}")
        return {
            "instance_id": instance_id,
            "status": "FAILED",
            "patch_file": None,
            "message": error_msg
        }

    # Write prompt file
    prompt_path = os.path.join(PROMPT_DIR, f"{instance_id}_prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(f"Task:\n{problem_statement}")

    # Cloud-native execution using the JAR
    command = [
        "java", "-jar", JAR_PATH,
        "/agent", "run",
        f"Read the issue description in {prompt_path}. Modify the codebase files to implement the fix."
    ]

    try:
        print(f"    Running ATROPOS in {WORKSPACE_DIR}...")
        result = subprocess.run(
            command,
            cwd=WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout per instance
        )

        # Check for refusal/failure in output regardless of exit code
        refusal_reason = check_atropos_output_for_refusal(result.stdout, result.stderr)

        if refusal_reason:
            print(f"    [REFUSED] {refusal_reason}")
            status = "REFUSED"
            message = refusal_reason
        elif result.returncode != 0:
            print(f"    [FAILED] Engine crashed with exit code {result.returncode}")
            print(f"    stderr: {result.stderr[:500]}")
            status = "FAILED"
            message = f"Engine crashed with exit code {result.returncode}: {result.stderr[:500]}"
        else:
            print(f"    [SUCCESS] ATROPOS completed, generating patch...")
            # Generate filtered patch
            patch_content, diff_success = run_git_diff_filtered(Path(WORKSPACE_DIR))
            if not diff_success:
                print(f"    [ERROR] {patch_content}")
                status = "FAILED"
                message = patch_content
            elif not patch_content.strip():
                print(f"    [EMPTY] No changes generated")
                status = "EMPTY_PATCH"
                message = "No changes generated by ATROPOS"
            else:
                # Write patch file
                with open(patch_file, "w", encoding="utf-8") as patch_out:
                    patch_out.write(patch_content)
                print(f"    [SUCCESS] Patch generated ({len(patch_content)} bytes)")
                status = "SUCCESS"
                message = "Patch generated successfully"

    except subprocess.TimeoutExpired:
        print(f"    [TIMEOUT] ATROPOS timed out after 5 minutes")
        status = "TIMEOUT"
        message = "ATROPOS execution timed out"
    except Exception as e:
        print(f"    [ERROR] Unexpected error: {e}")
        status = "ERROR"
        message = str(e)

    # Cleanup workspace
    shutil.rmtree(WORKSPACE_DIR, ignore_errors=True)

    return {
        "instance_id": instance_id,
        "status": status,
        "patch_file": patch_file if status == "SUCCESS" else None,
        "message": message
    }


def main():
    print("Fetching SWE-bench via HF API...")
    API_URL = "https://datasets-server.huggingface.co/rows?dataset=princeton-nlp/SWE-bench&config=default&split=test&offset={}&length=100"

    issues = []
    total_target = 2294

    for offset in range(0, total_target, 100):
        try:
            res = requests.get(API_URL.format(offset), timeout=30)
            if res.status_code == 200:
                for row in res.json()['rows']:
                    issues.append(row['row'])
            else:
                print(f"Warning: Failed to fetch offset {offset}: {res.status_code}")
            time.sleep(0.5)
        except Exception as e:
            print(f"Error fetching offset {offset}: {e}")

    print(f"\nLoaded {len(issues)} issues. Booting ATROPOS cloud pipeline...")

    results = []
    stats = {"SUCCESS": 0, "FAILED": 0, "REFUSED": 0, "EMPTY_PATCH": 0, "TIMEOUT": 0, "ERROR": 0, "SKIPPED": 0}

    for index, instance in enumerate(issues):
        result = process_instance(instance, index, len(issues))
        results.append(result)
        stats[result["status"]] = stats.get(result["status"], 0) + 1

        # Write predictions.jsonl incrementally
        if result["status"] == "SUCCESS" and result["patch_file"]:
            with open(result["patch_file"], "r", encoding="utf-8") as pf:
                patch_content = pf.read()
            prediction = {
                "instance_id": result["instance_id"],
                "model_patch": patch_content,
                "model_name_or_path": "ATROPOS"
            }
        else:
            prediction = {
                "instance_id": result["instance_id"],
                "model_patch": "",
                "model_name_or_path": "ATROPOS"
            }

        with open(PREDICTIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(prediction) + "\n")

        # Print progress
        print(f"    Progress: {stats}")

    # Final summary
    print("\n" + "="*50)
    print("EVALUATION COMPLETE")
    print("="*50)
    for status, count in stats.items():
        print(f"  {status}: {count}")
    print(f"  TOTAL: {sum(stats.values())}")
    print(f"\nPredictions written to: {PREDICTIONS_FILE}")


if __name__ == "__main__":
    main()