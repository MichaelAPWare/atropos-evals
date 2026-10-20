import os
import requests
import time
import subprocess
import shutil

# Paths adapted for GitHub Actions runner
WORKSPACE_DIR = os.path.abspath("tmp_workspace")
OUTPUT_DIR = os.path.abspath("predictions/swe-bench-full")
PROMPT_DIR = os.path.abspath("tmp_prompts")
JAR_PATH = os.path.abspath("ATROPOS.jar")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(PROMPT_DIR, exist_ok=True)

print("Fetching SWE-bench via HF API...")
API_URL = "https://" + "datasets-server.huggingface.co/rows?dataset=princeton-nlp/SWE-bench&config=default&split=test&offset={}&length=100"

issues = []
total_target = 2294

for offset in range(0, total_target, 100):
    res = requests.get(API_URL.format(offset))
    if res.status_code == 200:
        for row in res.json()['rows']:
            issues.append(row['row'])
    time.sleep(0.5)

print(f"\nLoaded {len(issues)} issues. Booting ATROPOS cloud pipeline...")

for index, instance in enumerate(issues):
    instance_id = instance['instance_id']
    repo = instance['repo']
    base_commit = instance['base_commit']
    problem_statement = instance['problem_statement']
    patch_file = os.path.join(OUTPUT_DIR, f"{instance_id}.patch")
    
    if os.path.exists(patch_file):
        continue
        
    print(f"\n[{index+1}/{len(issues)}] Handing {instance_id} to ATROPOS...")
    
    if os.path.exists(WORKSPACE_DIR): 
        shutil.rmtree(WORKSPACE_DIR, ignore_errors=True)
    os.makedirs(WORKSPACE_DIR, exist_ok=True)

    repo_url = "https://" + f"github.com/{repo}.git"
    try:
        subprocess.run(["git", "clone", repo_url, WORKSPACE_DIR], check=True, capture_output=True)
        subprocess.run(["git", "checkout", base_commit], cwd=WORKSPACE_DIR, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print(f"    [ERROR] Git clone failed.")
        continue

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
        subprocess.run(command, cwd=WORKSPACE_DIR, check=True)
        with open(patch_file, "w", encoding="utf-8") as patch_out:
            subprocess.run(["git", "diff"], cwd=WORKSPACE_DIR, stdout=patch_out, check=True)
        print(f"    [SUCCESS] Patch generated for {instance_id}")
    except subprocess.CalledProcessError:
        print(f"    [FAILED] Engine crashed on {instance_id}.")

    shutil.rmtree(WORKSPACE_DIR, ignore_errors=True)
